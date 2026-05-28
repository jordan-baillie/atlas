/**
 * ResearchTab -- "Funnel Analytics" (Variant D) rewrite.
 *
 * Replaces the Phase-pre experiment-centric research tab.  Treats the
 * research pipeline as a funnel (papers → filtered → specs → strategies)
 * and surfaces knowledge-layer signals (claims, contradictions, sources,
 * lifecycle transitions) alongside it.
 *
 * Drill-in stage selection is URL-synced via ?stage=<key>.
 *
 * Charts all go through the shared <Chart> wrapper (Chart.js).  See
 * design history in dashboard-ui/research-mockups/variant-d-funnel-analytics/.
 */

import { useMemo } from 'react'
import { Chart } from '../shared/Chart'
import { Sparkline } from '../shared/Sparkline'
import { Skeleton } from '../layout/Skeleton'
import { Badge } from '../shared/Badge'
import { SectionBoundary } from '../layout/SectionBoundary'
import { fmtRelativeTime, fmtNum, fmtPct } from '../../lib/format'
import { useUrlState } from '../../hooks/useUrlState'
import { FunnelChart, STAGE_DEFS, type StageKey } from './FunnelChart'

import {
  useOpenContradictions,
  useResolveContradiction,
  useKnowledgeSources,
  useContradictionsTimeline,
  useDigestHistory,
  useExtractionConfidence,
  useStrategySummaries,
  useDiscoveryFunnel,
  useQueueHealth,
} from '../../api/knowledge-queries'

import type {
  DiscoveryFunnelDay,
  OpenContradiction,
  Severity,
} from '../../api/knowledge-types'
import type { ChartData, ChartOptions } from 'chart.js'

// ──────────────────────────────────────────────────────────────────────────────
// Sticky pipeline header -- last digest, queue health, source counts
// ──────────────────────────────────────────────────────────────────────────────

function PipelineHeader() {
  const digestQ = useDigestHistory(30)
  const queueQ = useQueueHealth()
  const sourcesQ = useKnowledgeSources({ limit: 1 })  // total only

  const last = digestQ.data?.rows?.[digestQ.data.rows.length - 1]
  const lastTs = last?.sent_at
  const active = queueQ.data?.active ?? 0
  const queueBreakdown = queueQ.data?.by_status ?? {}
  const sourcesTotal = sourcesQ.data?.total ?? 0

  return (
    <div className="sticky top-0 z-10 -mx-2 px-4 py-2 bg-[var(--color-bg)]/85 backdrop-blur border-b border-[var(--color-border)]">
      <div className="flex items-center justify-between gap-4 flex-wrap text-xs">
        <div className="flex items-center gap-4 text-[var(--color-text-muted)]">
          <span>
            <span className="inline-block w-2 h-2 rounded-full bg-[var(--color-green)] mr-2" />
            last digest <span className="font-mono text-[var(--color-text)]">{lastTs ? fmtRelativeTime(lastTs) : '—'}</span>
          </span>
          <span>
            <span className="inline-block w-2 h-2 rounded-full bg-[var(--color-accent)] mr-2" />
            queue <span className="font-mono text-[var(--color-text)]">{active}</span>
            {Object.entries(queueBreakdown).length > 0 && (
              <span className="ml-2 text-[10px] text-[var(--color-text-muted)] font-mono">
                {Object.entries(queueBreakdown)
                  .filter(([s]) => ['queued', 'running', 'evaluating'].includes(s))
                  .map(([s, n]) => `${s[0]}${n}`)
                  .join(' ')}
              </span>
            )}
          </span>
          <span className="text-[var(--color-text-muted)]">
            sources <span className="font-mono text-[var(--color-text)]">{sourcesTotal}</span>
          </span>
        </div>
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
          Pipeline · Funnel Analytics
        </div>
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Stage health row -- 7d vs 30d pass-rate per stage with arrows
// ──────────────────────────────────────────────────────────────────────────────

function passRate(numerator: number, denominator: number): number | null {
  if (!denominator) return null
  return (numerator / denominator) * 100
}

function StageHealthRow({ funnel }: { funnel: DiscoveryFunnelDay[] }) {
  const stats = useMemo(() => {
    const last7 = funnel.slice(-7)
    const prior23 = funnel.slice(-30, -7)
    const sum = (xs: DiscoveryFunnelDay[], k: keyof DiscoveryFunnelDay) =>
      xs.reduce((acc, r) => acc + (r[k] as number || 0), 0)

    const stages: Array<{ label: string; key: keyof DiscoveryFunnelDay; prev?: keyof DiscoveryFunnelDay }> = [
      { label: 'FOUND',  key: 'papers_found' },
      { label: 'FILTER', key: 'papers_filtered',     prev: 'papers_found' },
      { label: 'SPECS',  key: 'specs_extracted',     prev: 'papers_filtered' },
      { label: 'STRAT',  key: 'strategies_generated', prev: 'specs_extracted' },
    ]
    return stages.map((s) => {
      const v7  = sum(last7,    s.key)
      const v23 = sum(prior23,  s.key)
      const dailyAvg = last7.length > 0 ? v7 / last7.length : 0
      let rate7: number | null = null
      let rate30: number | null = null
      if (s.prev) {
        rate7  = passRate(v7,  sum(last7,   s.prev))
        rate30 = passRate(v23, sum(prior23, s.prev))
      }
      const delta = (rate7 != null && rate30 != null) ? rate7 - rate30 : null
      return { ...s, v7, v23, dailyAvg, rate7, rate30, delta }
    })
  }, [funnel])

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-2 mt-4">
      {stats.map((s) => {
        const arrow = s.delta == null ? '→' : s.delta > 2 ? '↗' : s.delta < -2 ? '↘' : '→'
        const arrowColor = s.delta == null
          ? 'var(--color-text-muted)'
          : s.delta > 2
            ? 'var(--color-green)'
            : s.delta < -2
              ? 'var(--color-red)'
              : 'var(--color-text-muted)'

        return (
          <div key={s.label} className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg p-3">
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-mono mb-1">
              {s.label}
            </div>
            <div className="font-mono font-semibold text-lg">
              {s.rate7 != null ? `${s.rate7.toFixed(0)}%` : `${s.v7}`}
            </div>
            <div className="text-[10px] text-[var(--color-text-muted)] mt-1">
              <span style={{ color: arrowColor }}>{arrow}</span>{' '}
              {s.rate7 != null
                ? `vs ${s.rate30?.toFixed(0) ?? '—'}% (30d)`
                : `${s.dailyAvg.toFixed(1)}/day`}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Funnel timeseries -- 30d stacked area, optional stage highlight
// ──────────────────────────────────────────────────────────────────────────────

function FunnelTimeseries({
  funnel,
  highlightStage,
}: {
  funnel: DiscoveryFunnelDay[]
  highlightStage: StageKey | null
}) {
  const data = useMemo<ChartData<'line'>>(() => {
    const series: Array<{ key: keyof DiscoveryFunnelDay; label: string; color: string }> = [
      { key: 'papers_found',         label: 'Found',    color: '#3b82f6' },
      { key: 'papers_filtered',      label: 'Filtered', color: '#14b8a6' },
      { key: 'specs_extracted',      label: 'Specs',    color: '#6366f1' },
      { key: 'strategies_generated', label: 'Strategies', color: '#a855f7' },
    ]
    return {
      labels: funnel.map((r) => r.date),
      datasets: series.map((s) => {
        const isHighlight = highlightStage === null || highlightStage === s.key
        return {
          label: s.label,
          data: funnel.map((r) => (r[s.key] as number) ?? 0),
          borderColor: s.color,
          backgroundColor: `${s.color}33`,
          borderWidth: isHighlight ? 2 : 1,
          fill: false,
          tension: 0.25,
          pointRadius: 0,
          pointHoverRadius: 4,
          spanGaps: true,
          ...(isHighlight ? {} : { borderColor: `${s.color}55`, backgroundColor: `${s.color}11` }),
        }
      }),
    }
  }, [funnel, highlightStage])

  const options = useMemo<ChartOptions<'line'>>(() => ({
    plugins: { legend: { display: true } },
    scales: {
      x: {
        ticks: {
          color: 'var(--color-text-muted)',
          font: { size: 10 },
          autoSkipPadding: 24,
          maxRotation: 0,
        },
      },
      y: {
        beginAtZero: true,
        ticks: { color: 'var(--color-text-muted)', font: { size: 10 } },
      },
    },
    animation: { duration: 500 },
  }), [])

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
          Funnel — 30-day timeseries
        </h3>
        <span className="text-[10px] text-[var(--color-text-muted)]">
          {highlightStage ? `Highlighting ${highlightStage}` : 'All stages · click a funnel stage above to isolate'}
        </span>
      </div>
      <Chart
        kind="line"
        data={data as ChartData<'line' | 'bar' | 'doughnut'>}
        options={options as ChartOptions<'line' | 'bar' | 'doughnut'>}
        height={260}
      />
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Contradictions over time + Resolution velocity (two-up row)
// ──────────────────────────────────────────────────────────────────────────────

function ContradictionsChartsRow() {
  const tQ = useContradictionsTimeline(30)

  const stackedData = useMemo<ChartData<'line'>>(() => {
    const timeline = tQ.data?.timeline ?? []
    return {
      labels: timeline.map((t) => t.date),
      datasets: [
        { label: 'Critical', data: timeline.map((t) => t.critical), borderColor: '#ef4444', backgroundColor: '#ef444433', fill: true, tension: 0.2, pointRadius: 0 },
        { label: 'Major',    data: timeline.map((t) => t.major),    borderColor: '#f59e0b', backgroundColor: '#f59e0b33', fill: true, tension: 0.2, pointRadius: 0 },
        { label: 'Minor',    data: timeline.map((t) => t.minor),    borderColor: '#8b929d', backgroundColor: '#8b929d33', fill: true, tension: 0.2, pointRadius: 0 },
      ],
    }
  }, [tQ.data])

  const stackedOpts = useMemo<ChartOptions<'line'>>(() => ({
    plugins: { legend: { display: true } },
    scales: {
      x: { ticks: { color: 'var(--color-text-muted)', font: { size: 10 }, maxRotation: 0, autoSkipPadding: 16 } },
      y: { stacked: true, beginAtZero: true, ticks: { color: 'var(--color-text-muted)', font: { size: 10 } } },
    },
    animation: { duration: 400 },
  }), [])

  const velocityData = useMemo<ChartData<'bar'>>(() => {
    const timeline = tQ.data?.timeline ?? []
    // 4 weekly buckets ending today
    const buckets: Array<{ label: string; resolved: number }> = []
    const now = new Date()
    for (let w = 3; w >= 0; w--) {
      const end = new Date(now); end.setDate(now.getDate() - w * 7)
      const start = new Date(end); start.setDate(end.getDate() - 6)
      const startStr = start.toISOString().slice(0, 10)
      const endStr = end.toISOString().slice(0, 10)
      const resolved = timeline
        .filter((t) => t.date >= startStr && t.date <= endStr)
        .reduce((s, t) => s + (t.resolved ?? 0), 0)
      buckets.push({ label: `wk -${w}`, resolved })
    }
    return {
      labels: buckets.map((b) => b.label),
      datasets: [
        {
          label: 'Resolved',
          data: buckets.map((b) => b.resolved),
          backgroundColor: '#22c55e',
        },
      ],
    }
  }, [tQ.data])

  const velocityOpts = useMemo<ChartOptions<'bar'>>(() => ({
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { color: 'var(--color-text-muted)', font: { size: 10 } } },
      y: { beginAtZero: true, ticks: { color: 'var(--color-text-muted)', font: { size: 10 } } },
    },
    animation: { duration: 400 },
  }), [])

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-3">
          Contradictions over time (by severity)
        </h3>
        <Chart
          kind="line"
          data={stackedData as ChartData<'line' | 'bar' | 'doughnut'>}
          options={stackedOpts as ChartOptions<'line' | 'bar' | 'doughnut'>}
          height={220}
        />
      </div>
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-3">
          Resolution velocity — last 4 weeks
        </h3>
        <Chart
          kind="bar"
          data={velocityData as ChartData<'line' | 'bar' | 'doughnut'>}
          options={velocityOpts as ChartOptions<'line' | 'bar' | 'doughnut'>}
          height={220}
        />
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Drill-in panel -- view depends on selected stage
// ──────────────────────────────────────────────────────────────────────────────

function StageDrillIn({
  stage,
  onClearStage,
  funnel,
}: {
  stage: StageKey | null
  onClearStage: () => void
  funnel: DiscoveryFunnelDay[]
}) {
  const digestQ = useDigestHistory(14)
  const sourcesQ = useKnowledgeSources({ limit: 8 }, stage === 'specs_extracted' || stage === null)
  const strategiesQ = useStrategySummaries(stage === 'strategies_generated')

  const last7 = funnel.slice(-7)
  const last30 = funnel.slice(-30)

  let pill = 'DIGEST'
  let heading = 'Discovery digest — recent days'
  let body: React.ReactNode = null

  if (stage === null) {
    body = (
      <div className="space-y-2 text-xs">
        {digestQ.data?.rows?.slice(-7).reverse().map((d) => (
          <div key={d.id} className="flex items-center justify-between border-b border-[var(--color-border)]/50 pb-1">
            <span className="text-[var(--color-text-muted)] font-mono">{d.sent_at.slice(0, 10)}</span>
            <span className="flex gap-3 font-mono">
              <span title="papers">📄 {d.new_papers}</span>
              <span title="contradictions">⚠ {d.new_contradictions}</span>
              <span title="lifecycle">🔁 {d.lifecycle_transitions}</span>
              <Badge variant={d.delivery_status === 'ok' ? 'success' : 'danger'} size="xs">{d.delivery_status ?? '—'}</Badge>
            </span>
          </div>
        ))}
      </div>
    )
  } else if (stage === 'papers_found' || stage === 'papers_filtered') {
    pill = stage === 'papers_found' ? 'FOUND' : 'FILTER'
    heading = stage === 'papers_found'
      ? 'Fetcher activity — last 7 / 30 days'
      : 'Filter pass rate — last 7 / 30 days'
    body = (
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <Stat label="7d found"   value={last7.reduce((s, r) => s + r.papers_found, 0)} />
        <Stat label="7d filtered" value={last7.reduce((s, r) => s + r.papers_filtered, 0)} />
        <Stat label="30d found"  value={last30.reduce((s, r) => s + r.papers_found, 0)} />
        <Stat label="30d filtered" value={last30.reduce((s, r) => s + r.papers_filtered, 0)} />
      </div>
    )
  } else if (stage === 'specs_extracted') {
    pill = 'SPECS'
    heading = 'Most recent ingested sources'
    body = (
      <div className="space-y-1 text-xs max-h-[300px] overflow-y-auto">
        {sourcesQ.data?.rows?.slice(0, 10).map((s) => (
          <div key={s.id} className="flex items-center justify-between border-b border-[var(--color-border)]/40 py-1">
            <span className="truncate mr-2">
              {s.url ? <a href={s.url} className="text-[var(--color-accent)] hover:underline" target="_blank" rel="noreferrer">{s.title}</a> : s.title}
            </span>
            <span className="flex gap-3 font-mono flex-shrink-0">
              <span title="claims">{s.claim_count}c</span>
              <span title="open contradictions" style={{ color: s.open_contradictions > 0 ? 'var(--color-amber, #f59e0b)' : 'var(--color-text-muted)' }}>{s.open_contradictions}⚠</span>
              <span className="text-[var(--color-text-muted)] text-[10px]">{fmtRelativeTime(s.ingested_at)}</span>
            </span>
          </div>
        ))}
      </div>
    )
  } else if (stage === 'strategies_generated') {
    pill = 'STRAT'
    heading = 'Top strategies by open contradictions'
    body = (
      <div className="space-y-1 text-xs max-h-[300px] overflow-y-auto">
        {strategiesQ.data?.rows?.slice(0, 10).map((s) => (
          <div key={`${s.strategy}-${s.universe}`} className="flex items-center justify-between border-b border-[var(--color-border)]/40 py-1">
            <span>
              <span className="font-medium">{s.strategy}</span>
              <Badge variant="neutral" size="xs" className="ml-2">{s.universe}</Badge>
              {s.lifecycle_state && (
                <Badge variant="neutral" size="xs" className="ml-1">{s.lifecycle_state}</Badge>
              )}
            </span>
            <span className="font-mono flex gap-3">
              <span>sharpe {s.solo_sharpe != null ? s.solo_sharpe.toFixed(2) : '—'}</span>
              <span style={{ color: s.open_contradictions > 0 ? 'var(--color-amber, #f59e0b)' : 'var(--color-text-muted)' }}>
                {s.open_contradictions}⚠
              </span>
            </span>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <div className="flex items-center gap-3">
          <Badge variant="neutral" size="xs">{pill}</Badge>
          <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
            {heading}
          </h3>
        </div>
        {stage != null && (
          <button
            onClick={onClearStage}
            className="text-xs text-[var(--color-text-muted)] hover:text-[var(--color-text)] underline-offset-2 hover:underline"
          >
            Reset selection
          </button>
        )}
      </div>
      {body}
    </div>
  )
}

function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded-md p-2">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-mono">{label}</div>
      <div className="text-base font-mono font-semibold">{typeof value === 'number' ? fmtNum(value) : value}</div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Pipeline diagnostics -- text callouts computed from data
// ──────────────────────────────────────────────────────────────────────────────

interface Callout { tone: 'ok' | 'warn' | 'info'; text: string }

function PipelineDiagnostics({ funnel }: { funnel: DiscoveryFunnelDay[] }) {
  const extractionQ = useExtractionConfidence()
  const tQ = useContradictionsTimeline(30)
  const queueQ = useQueueHealth()
  const digestQ = useDigestHistory(7)

  const callouts: Callout[] = useMemo(() => {
    const out: Callout[] = []
    const last7  = funnel.slice(-7)
    const prior23 = funnel.slice(-30, -7)
    const sum = (xs: DiscoveryFunnelDay[], k: keyof DiscoveryFunnelDay) =>
      xs.reduce((acc, r) => acc + (r[k] as number || 0), 0)

    const foundAvg = last7.length > 0 ? sum(last7, 'papers_found') / last7.length : 0
    out.push({ tone: foundAvg >= 5 ? 'ok' : 'warn',
      text: `Fetcher avg: ${foundAvg.toFixed(1)} papers/day over the last 7 days.` })

    // Filter pass rate delta
    const filtRate7  = passRate(sum(last7,   'papers_filtered'), sum(last7,   'papers_found'))
    const filtRate30 = passRate(sum(prior23, 'papers_filtered'), sum(prior23, 'papers_found'))
    if (filtRate7 != null && filtRate30 != null) {
      const delta = filtRate7 - filtRate30
      if (Math.abs(delta) >= 5) {
        out.push({
          tone: delta < 0 ? 'warn' : 'ok',
          text: `Filter pass-rate ${delta < 0 ? 'dropped' : 'rose'}: ${filtRate7.toFixed(0)}% this week vs ${filtRate30.toFixed(0)}% prior 23 days.`,
        })
      } else {
        out.push({ tone: 'ok', text: `Filter pass-rate stable around ${filtRate7.toFixed(0)}%.` })
      }
    }

    // Zero-spec days
    const zeroSpecDays = last7.filter((r) => r.specs_extracted === 0).map((r) => r.date)
    if (zeroSpecDays.length === 1) {
      out.push({ tone: 'info', text: `One day with 0 specs (${zeroSpecDays[0]}) — likely an arxiv rate-limit or all-off-topic batch.` })
    } else if (zeroSpecDays.length > 1) {
      out.push({ tone: 'warn', text: `${zeroSpecDays.length} days with 0 specs in the last 7 — investigate filter or fetch.` })
    }

    // Critical contradictions
    const criticalTotal = (tQ.data?.timeline ?? []).reduce((s, t) => s + t.critical, 0)
    if (criticalTotal > 0) {
      out.push({ tone: 'warn', text: `${criticalTotal} critical contradictions opened in the last 30 days.` })
    }

    // Extraction confidence
    const conf = extractionQ.data
    if (conf && conf.total > 0) {
      const lowPct = (conf.histogram.low / conf.total) * 100
      if (lowPct > 25) {
        out.push({ tone: 'warn', text: `${lowPct.toFixed(0)}% of extracted claims marked low-confidence — prompt may need tuning.` })
      } else {
        out.push({ tone: 'ok', text: `Extraction confidence: ${conf.histogram.high}H / ${conf.histogram.medium}M / ${conf.histogram.low}L over ${conf.total} claims.` })
      }
    }

    // Queue depth
    const qActive = queueQ.data?.active ?? 0
    if (qActive > 25) {
      out.push({ tone: 'warn', text: `Queue depth ${qActive} — backtester may be falling behind.` })
    } else if (qActive > 0) {
      out.push({ tone: 'ok', text: `Queue depth ${qActive} (${queueQ.data?.source}) — backtester keeping pace.` })
    }

    // Digest delivery
    const recentDigests = digestQ.data?.rows ?? []
    const failed = recentDigests.filter((d) => d.delivery_status && d.delivery_status !== 'ok')
    if (failed.length > 0) {
      out.push({ tone: 'warn', text: `${failed.length} digest delivery failure${failed.length === 1 ? '' : 's'} in the last 7 days.` })
    }

    return out
  }, [funnel, extractionQ.data, tQ.data, queueQ.data, digestQ.data])

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium mb-3">
        Pipeline diagnostics
      </h3>
      <div className="space-y-1.5 text-xs">
        {callouts.map((c, i) => (
          <div key={i} className="flex items-start gap-2">
            <span
              className="font-mono w-4 flex-shrink-0"
              style={{
                color: c.tone === 'ok' ? 'var(--color-green)'
                  : c.tone === 'warn' ? 'var(--color-amber, #f59e0b)'
                  : 'var(--color-accent)',
              }}
            >
              {c.tone === 'ok' ? '✓' : c.tone === 'warn' ? '⚠' : 'ℹ'}
            </span>
            <span>{c.text}</span>
          </div>
        ))}
        {callouts.length === 0 && (
          <div className="text-[var(--color-text-muted)]">No diagnostics yet — collecting data.</div>
        )}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Top contradictions table + Source inventory (two-up bottom row)
// ──────────────────────────────────────────────────────────────────────────────

function severityColor(s: Severity): string {
  return s === 'critical' ? 'var(--color-red)' : s === 'major' ? 'var(--color-amber, #f59e0b)' : 'var(--color-text-muted)'
}

function TopContradictionsTable() {
  const q = useOpenContradictions({ limit: 10 })
  const resolveMut = useResolveContradiction()
  const rows = q.data?.rows ?? []

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="px-4 py-3 border-b border-[var(--color-border)] flex items-center justify-between">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
          Top open contradictions
        </h3>
        <span className="text-[10px] text-[var(--color-text-muted)] font-mono">{q.data?.count ?? 0}</span>
      </div>
      <div className="overflow-x-auto max-h-[420px]">
        <table className="w-full text-xs">
          <thead className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
            <tr className="border-b border-[var(--color-border)]">
              <th className="text-left px-3 py-2 font-medium">Sev</th>
              <th className="text-left px-3 py-2 font-medium">Strategy</th>
              <th className="text-left px-3 py-2 font-medium">Metric</th>
              <th className="text-right px-3 py-2 font-medium">Claimed</th>
              <th className="text-right px-3 py-2 font-medium">Measured</th>
              <th className="text-right px-3 py-2 font-medium">Δ</th>
              <th className="text-left px-3 py-2 font-medium">Source</th>
              <th className="text-right px-3 py-2 font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <ContradictionRow
                key={row.contradiction_id}
                row={row}
                onResolve={(resolution, note) =>
                  resolveMut.mutate({ id: row.contradiction_id, resolution, note })
                }
              />
            ))}
            {!q.isLoading && rows.length === 0 && (
              <tr>
                <td colSpan={8} className="px-3 py-6 text-center text-[var(--color-text-muted)]">
                  Inbox zero. No open contradictions.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

function ContradictionRow({ row, onResolve }: {
  row: OpenContradiction
  onResolve: (resolution: 'retested' | 'claim_rejected' | 'measurement_corrected' | 'deferred', note?: string) => void
}) {
  return (
    <tr className="border-b border-[var(--color-border)]/40 hover:bg-[var(--color-surface-alt)]">
      <td className="px-3 py-2">
        <span className="font-mono text-[10px]" style={{ color: severityColor(row.severity) }}>
          {row.severity.toUpperCase()}
        </span>
      </td>
      <td className="px-3 py-2">
        <span className="font-medium">{row.strategy}</span>
        <Badge variant="neutral" size="xs" className="ml-2">{row.universe}</Badge>
      </td>
      <td className="px-3 py-2 font-mono text-[var(--color-text-muted)]">{row.metric}</td>
      <td className="px-3 py-2 text-right font-mono">{row.claimed_value?.toFixed(2) ?? '—'}</td>
      <td className="px-3 py-2 text-right font-mono">{row.measured_value?.toFixed(2) ?? '—'}</td>
      <td className="px-3 py-2 text-right font-mono" style={{ color: severityColor(row.severity) }}>
        {row.delta != null ? (row.delta > 0 ? '+' : '') + row.delta.toFixed(2) : '—'}
      </td>
      <td className="px-3 py-2 max-w-[240px] truncate">
        {row.source_url ? (
          <a href={row.source_url} target="_blank" rel="noreferrer" className="text-[var(--color-accent)] hover:underline">
            {row.source_title ?? row.source_id}
          </a>
        ) : (
          row.source_title ?? row.source_id ?? '—'
        )}
      </td>
      <td className="px-3 py-2 text-right">
        <select
          className="text-[10px] bg-[var(--color-surface-alt)] border border-[var(--color-border)] rounded px-1 py-0.5 cursor-pointer"
          onChange={(e) => {
            const v = e.target.value
            if (v) {
              onResolve(v as 'retested' | 'claim_rejected' | 'measurement_corrected' | 'deferred')
              e.target.value = ''
            }
          }}
          defaultValue=""
        >
          <option value="">Resolve…</option>
          <option value="retested">Retested</option>
          <option value="claim_rejected">Reject claim</option>
          <option value="measurement_corrected">Correct measurement</option>
          <option value="deferred">Defer</option>
        </select>
      </td>
    </tr>
  )
}

function SourceInventoryTable() {
  const q = useKnowledgeSources({ limit: 10 })
  const rows = q.data?.rows ?? []
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl dash-card overflow-hidden">
      <div className="px-4 py-3 border-b border-[var(--color-border)] flex items-center justify-between">
        <h3 className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-medium">
          Source inventory — recent
        </h3>
        <span className="text-[10px] text-[var(--color-text-muted)] font-mono">{q.data?.total ?? 0} total</span>
      </div>
      <div className="overflow-x-auto max-h-[420px]">
        <table className="w-full text-xs">
          <thead className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
            <tr className="border-b border-[var(--color-border)]">
              <th className="text-left px-3 py-2 font-medium">Title</th>
              <th className="text-left px-3 py-2 font-medium">Venue</th>
              <th className="text-right px-3 py-2 font-medium">Claims</th>
              <th className="text-right px-3 py-2 font-medium">Open ⚠</th>
              <th className="text-right px-3 py-2 font-medium">Ingested</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.id} className="border-b border-[var(--color-border)]/40 hover:bg-[var(--color-surface-alt)]">
                <td className="px-3 py-2 max-w-[280px] truncate">
                  {s.url ? (
                    <a href={s.url} target="_blank" rel="noreferrer" className="text-[var(--color-accent)] hover:underline">
                      {s.title}
                    </a>
                  ) : s.title}
                </td>
                <td className="px-3 py-2 text-[var(--color-text-muted)]">{s.venue ?? '—'}</td>
                <td className="px-3 py-2 text-right font-mono">{s.claim_count}</td>
                <td className="px-3 py-2 text-right font-mono" style={{ color: s.open_contradictions > 0 ? 'var(--color-amber, #f59e0b)' : 'var(--color-text-muted)' }}>
                  {s.open_contradictions}
                </td>
                <td className="px-3 py-2 text-right font-mono text-[var(--color-text-muted)] text-[10px]">{fmtRelativeTime(s.ingested_at)}</td>
              </tr>
            ))}
            {!q.isLoading && rows.length === 0 && (
              <tr><td colSpan={5} className="px-3 py-6 text-center text-[var(--color-text-muted)]">No sources ingested yet.</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// KPI strip (small) -- digest sparkline + counts
// ──────────────────────────────────────────────────────────────────────────────

function KpiStrip() {
  const digestQ = useDigestHistory(30)
  const openQ = useOpenContradictions({ limit: 1 })
  const sourcesQ = useKnowledgeSources({ limit: 1 })
  const strategiesQ = useStrategySummaries()

  const sparkData = (digestQ.data?.rows ?? []).map((d) => d.new_contradictions)
  const claimsWithMetrics = strategiesQ.data?.rows?.reduce((s, r) => s + (r.active_claims ?? 0), 0) ?? 0

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      <KpiCard label="Open contradictions" value={openQ.data?.count ?? 0} color="amber" />
      <KpiCard label="Sources" value={sourcesQ.data?.total ?? 0} />
      <KpiCard label="Active claims" value={claimsWithMetrics} />
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 dash-card">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">Contradictions opened (30d)</div>
        <Sparkline data={sparkData} height={42} />
      </div>
    </div>
  )
}

function KpiCard({ label, value, color }: { label: string; value: number | string; color?: 'amber' | 'red' }) {
  const tone = color === 'amber' ? 'var(--color-amber, #f59e0b)' : color === 'red' ? 'var(--color-red)' : undefined
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 dash-card">
      <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">{label}</div>
      <div className="text-2xl font-mono font-semibold" style={tone ? { color: tone } : undefined}>{value}</div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────────────────
// Main composition
// ──────────────────────────────────────────────────────────────────────────────

export function ResearchTab() {
  const funnelQ = useDiscoveryFunnel(30)
  const [stage, setStage] = useUrlState<StageKey | null>('stage', null)

  const funnel = funnelQ.data?.funnel ?? []

  if (funnelQ.isLoading && funnel.length === 0) {
    return <Skeleton className="h-96" />
  }

  return (
    <div className="space-y-4 md:space-y-6 stagger">
      <SectionBoundary name="pipeline-header">
        <PipelineHeader />
      </SectionBoundary>

      <SectionBoundary name="kpis">
        <KpiStrip />
      </SectionBoundary>

      <SectionBoundary name="funnel">
        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card animate-in">
          <FunnelChart funnel={funnel} selected={stage} onSelect={setStage} />
          <StageHealthRow funnel={funnel} />
        </div>
      </SectionBoundary>

      <SectionBoundary name="funnel-timeseries">
        <FunnelTimeseries funnel={funnel} highlightStage={stage} />
      </SectionBoundary>

      <SectionBoundary name="contradictions-charts">
        <ContradictionsChartsRow />
      </SectionBoundary>

      <SectionBoundary name="drill-in">
        <StageDrillIn stage={stage} onClearStage={() => setStage(null)} funnel={funnel} />
      </SectionBoundary>

      <SectionBoundary name="diagnostics">
        <PipelineDiagnostics funnel={funnel} />
      </SectionBoundary>

      <SectionBoundary name="bottom-tables">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          <TopContradictionsTable />
          <SourceInventoryTable />
        </div>
      </SectionBoundary>
    </div>
  )
}

// Suppress unused-import lint for fmtPct which may be needed by future polishes.
void fmtPct
