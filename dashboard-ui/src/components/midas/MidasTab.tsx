/**
 * MidasTab — Cross-sectional funding-carry PAPER strategy (simulation only).
 *
 * Reads /api/midas (paper-engine outputs) and shows returns + positions/trades.
 * Styling mirrors the Portfolio tab: token-based surfaces, StatCard KPIs, the
 * shared Chart wrapper for the equity curve, and DataTable for holdings/trades.
 */
import { useEffect, useMemo, useState } from 'react'
import { StatCard } from '../shared/StatCard'
import { Chart } from '../shared/Chart'
import { DataTable, type Column } from '../shared/DataTable'
import { Skeleton } from '../layout/Skeleton'

interface Stats {
  n?: number
  ann_return?: number
  ann_sharpe?: number
  cum_return?: number
  max_dd?: number
}
interface Holding { symbol: string; weight: number; funding_signal: number }
interface Rebalance { date: string; n_long: number; n_short: number; turnover: number }
interface DemoPos { symbol: string; qty: number; notional_usd: number; entry_price: number; unrealized_pnl: number }
interface Demo {
  running: boolean
  endpoint?: string
  capital_usd?: number
  inception_date?: string
  last_run_ts?: string
  equity_usd?: number
  pnl_usd?: number
  realized_pnl_usd?: number
  gross_usd?: number
  n_positions?: number
  n_target?: number
  n_placed?: number
  n_skipped?: number
  n_errors?: number
  kill_present?: boolean
  as_of?: string
  equity_curve?: Array<{ date: string; equity: number; pnl: number }>
  positions?: { n_long: number; n_short: number; longs: DemoPos[]; shorts: DemoPos[] }
}
interface MidasPayload {
  strategy: string
  mode: string
  venue_data: string
  as_of?: string
  inception?: string
  universe_names_latest?: number
  stats: { forward_since_inception: Stats; full_backtest: Stats }
  decomposition: { carry_ann?: number; price_ann?: number; cost_ann?: number; net_ann?: number }
  equity: Array<{ date: string; equity: number }>
  positions: {
    as_of?: string
    n_long?: number
    n_short?: number
    longs?: Holding[]
    shorts?: Holding[]
    recent_rebalances?: Rebalance[]
  }
  demo?: Demo | null
  disclaimer: string
}

const pct = (x?: number) => (x == null ? '—' : `${(x * 100).toFixed(1)}%`)
const sharpe = (x?: number) => (x == null ? '—' : x.toFixed(2))
const signColor = (x?: number) => (x == null ? undefined : x >= 0 ? '#22c55e' : '#ef4444')
const wpct = (x: number) => `${(x * 100).toFixed(2)}%`
const fundbps = (x: number) => `${(x * 10000).toFixed(1)} bps`
const usd = (x?: number) => (x == null ? '—' : `${x >= 0 ? '+' : '-'}$${Math.abs(x).toFixed(2)}`)
const usd0 = (x?: number) => (x == null ? '—' : `$${Math.round(x).toLocaleString()}`)

const demoCols: Column<DemoPos>[] = [
  { key: 'symbol', label: 'Symbol', render: (r) => <span className="font-mono">{r.symbol.replace(/USDT$/, '')}</span> },
  { key: 'notional_usd', label: 'Notional', align: 'right', render: (r) => <span className="font-mono tabular-nums">{usd0(r.notional_usd)}</span> },
  { key: 'unrealized_pnl', label: 'uPnL', align: 'right', render: (r) => (
    <span className="font-mono tabular-nums" style={{ color: signColor(r.unrealized_pnl) }}>{usd(r.unrealized_pnl)}</span>
  ) },
]

// Auto-refresh cadence. Matches the /api/midas server cache TTL (120s): polling
// faster just returns cached bytes. Underlying data only changes once daily
// (midas-demo.timer, 08:15 UTC), so this mainly lets an open tab pick up the
// daily run without a manual reload.
const REFRESH_MS = 120_000

function useMidas() {
  const [data, setData] = useState<MidasPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  useEffect(() => {
    let cancelled = false
    let ac: AbortController | null = null
    const load = () => {
      // Don't poll while the tab is backgrounded.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      ac?.abort()
      ac = new AbortController()
      fetch('/api/midas', { credentials: 'same-origin', signal: ac.signal })
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((d: MidasPayload) => {
          if (cancelled) return
          setData(d)
          setError(null)
          setUpdatedAt(Date.now())
        })
        .catch((e) => { if (!cancelled && e.name !== 'AbortError') setError(String(e.message ?? e)) })
    }
    load()
    const id = setInterval(load, REFRESH_MS)
    // Refresh immediately when the tab regains focus (e.g. after the daily run).
    const onVis = () => { if (document.visibilityState === 'visible') load() }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      cancelled = true
      ac?.abort()
      clearInterval(id)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [])
  return { data, error, updatedAt }
}

const clock = (ts: number) =>
  new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3 py-2">
      <div className="h-px flex-1 bg-[var(--color-border)]" />
      <span className="text-[10px] uppercase tracking-[0.15em] text-[var(--color-text-muted)] font-semibold">
        {children}
      </span>
      <div className="h-px flex-1 bg-[var(--color-border)]" />
    </div>
  )
}

const holdingCols: Column<Holding>[] = [
  { key: 'symbol', label: 'Symbol', render: (r) => <span className="font-mono">{r.symbol.replace(/USDT$/, '')}</span> },
  { key: 'weight', label: 'Weight', align: 'right', render: (r) => <span className="font-mono tabular-nums">{wpct(r.weight)}</span> },
  { key: 'funding_signal', label: 'Funding (3d)', align: 'right', render: (r) => (
    <span className="font-mono tabular-nums" style={{ color: signColor(r.funding_signal) }}>{fundbps(r.funding_signal)}</span>
  ) },
]

const rebalCols: Column<Rebalance>[] = [
  { key: 'date', label: 'Date', render: (r) => <span className="font-mono">{r.date}</span> },
  { key: 'n_long', label: 'Long', align: 'right', render: (r) => <span className="font-mono tabular-nums text-green-400">{r.n_long}</span> },
  { key: 'n_short', label: 'Short', align: 'right', render: (r) => <span className="font-mono tabular-nums text-red-400">{r.n_short}</span> },
  { key: 'turnover', label: 'Turnover', align: 'right', render: (r) => <span className="font-mono tabular-nums">{wpct(r.turnover)}</span> },
]

export function MidasTab() {
  const { data, error, updatedAt } = useMidas()

  const equityChart = useMemo(() => {
    if (!data?.equity?.length) return null
    return {
      labels: data.equity.map((p) => p.date),
      datasets: [{ label: 'Equity (×)', data: data.equity.map((p) => p.equity), color: 'green' }],
    }
  }, [data])

  const demoChart = useMemo(() => {
    const dq = data?.demo?.equity_curve
    if (!dq?.length) return null
    return {
      labels: dq.map((p) => p.date),
      datasets: [{ label: 'Demo P&L ($)', data: dq.map((p) => p.pnl), color: 'blue' }],
    }
  }, [data])

  if (error) {
    return (
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-6 text-sm text-[var(--color-text-muted)]">
        Could not load Midas data: {error}
      </div>
    )
  }
  if (!data) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-24" />
        <Skeleton className="h-80" />
      </div>
    )
  }

  const full = data.stats.full_backtest ?? {}
  const fwd = data.stats.forward_since_inception ?? {}
  const d = data.decomposition ?? {}
  const longs = data.positions?.longs ?? []
  const shorts = data.positions?.shorts ?? []
  const demo = data.demo

  return (
    <div className="space-y-4 md:space-y-6">
      {/* Header */}
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-base font-semibold text-[var(--color-text)]">{data.strategy}</div>
            <div className="text-xs text-[var(--color-text-muted)] mt-0.5">{data.venue_data}</div>
          </div>
          <div className="flex items-center gap-2">
            {updatedAt != null && (
              <span
                className="inline-flex items-center gap-1.5 text-[10px] text-[var(--color-text-muted)] font-mono"
                title={`Auto-refreshes every ${Math.round(REFRESH_MS / 1000)}s`}
              >
                <span className="h-1.5 w-1.5 rounded-full bg-green-500 animate-pulse" />
                updated {clock(updatedAt)}
              </span>
            )}
            <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-semibold uppercase tracking-wider bg-amber-500/10 text-amber-400 border border-amber-500/20">
              {data.mode}
            </span>
          </div>
        </div>
        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[11px] text-[var(--color-text-muted)] font-mono">
          <span>as of {data.as_of ?? '—'}</span>
          <span>inception {data.inception ?? '—'}</span>
          <span>{data.universe_names_latest ?? '—'} names ({data.positions?.n_long ?? 0}L / {data.positions?.n_short ?? 0}S)</span>
        </div>
      </div>

      {/* Live demo execution (Bybit api-demo, zero real capital) */}
      {demo?.running && (
        <>
          <SectionLabel>Live Demo Execution — Bybit (api-demo · zero real capital)</SectionLabel>
          <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2">
                <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-semibold uppercase tracking-wider bg-sky-500/10 text-sky-400 border border-sky-500/20">
                  ● Live Demo
                </span>
                {demo.kill_present && (
                  <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-semibold uppercase tracking-wider bg-red-500/15 text-red-400 border border-red-500/30">KILL active</span>
                )}
                {!!demo.n_errors && !demo.kill_present && (
                  <span className="inline-flex items-center px-2 py-1 rounded text-[10px] font-semibold uppercase tracking-wider bg-amber-500/15 text-amber-400 border border-amber-500/30">{demo.n_errors} errors</span>
                )}
              </div>
              <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-[var(--color-text-muted)] font-mono">
                <span>{demo.endpoint?.replace('https://', '')}</span>
                <span>demo cap {usd0(demo.capital_usd)}</span>
                <span>since {demo.inception_date ?? '—'}</span>
                <span>as of {demo.as_of ?? '—'}</span>
              </div>
            </div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
              <StatCard label="Demo P&L" value={usd(demo.pnl_usd)} hero accent={signColor(demo.pnl_usd)}
                        sub={`realized ${usd(demo.realized_pnl_usd)}`} subColor="neutral" />
              <StatCard label="Gross Exposure" value={usd0(demo.gross_usd)} sub={`${((demo.gross_usd ?? 0) / (demo.capital_usd || 1)).toFixed(2)}× capital`} subColor="neutral" />
              <StatCard label="Positions" value={`${demo.n_positions ?? 0}`} sub={`${demo.positions?.n_long ?? 0}L / ${demo.positions?.n_short ?? 0}S`} subColor="neutral" />
              <StatCard label="Last Rebalance" value={`${demo.n_placed ?? 0} orders`} sub={`${demo.n_target ?? 0} target · ${demo.n_skipped ?? 0} unlisted`} subColor="neutral" />
            </div>
            {demoChart && (
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold mb-1">Realized + Unrealized P&L ($)</div>
                <Chart kind="line" data={demoChart} height={220}
                       options={{ scales: { x: { ticks: { maxTicksLimit: 8 } }, y: { title: { display: true, text: 'P&L ($)' } } } }} />
              </div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 md:gap-4">
              <div>
                <div className="text-[11px] uppercase tracking-wider text-green-400 font-semibold mb-2">Longs · {demo.positions?.n_long ?? 0}</div>
                <DataTable columns={demoCols} data={demo.positions?.longs ?? []} density="compact" emptyMessage="No long positions" />
              </div>
              <div>
                <div className="text-[11px] uppercase tracking-wider text-red-400 font-semibold mb-2">Shorts · {demo.positions?.n_short ?? 0}</div>
                <DataTable columns={demoCols} data={demo.positions?.shorts ?? []} density="compact" emptyMessage="No short positions" />
              </div>
            </div>
          </div>
        </>
      )}

      {/* KPI row — full backtest */}
      <SectionLabel>Backtest (survivorship-clean, 2020–present)</SectionLabel>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
        <StatCard label="Sharpe" value={sharpe(full.ann_sharpe)} hero accent={signColor(full.ann_sharpe)} />
        <StatCard label="Ann. Return" value={pct(full.ann_return)} accent={signColor(full.ann_return)}
                  sub={`net ${pct(d.net_ann)}`} subColor={(d.net_ann ?? 0) >= 0 ? 'positive' : 'negative'} />
        <StatCard label="Max Drawdown" value={pct(full.max_dd)} accent="#ef4444" sub="2022 crisis year" subColor="neutral" />
        <StatCard label="Cum. Return" value={pct(full.cum_return)} accent={signColor(full.cum_return)}
                  sub={`${full.n ?? 0} days`} subColor="neutral" />
      </div>

      {/* Decomposition + forward */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 md:gap-4">
        <StatCard label="Carry (ann)" value={pct(d.carry_ann)} subColor="positive" sub="funding harvested" />
        <StatCard label="Price (ann)" value={pct(d.price_ann)} subColor={(d.price_ann ?? 0) >= 0 ? 'positive' : 'negative'} sub="positioning" />
        <StatCard label="Cost (ann)" value={pct(d.cost_ann)} subColor="negative" sub="turnover" />
        <StatCard label="Forward (live)" value={fwd.n ? sharpe(fwd.ann_sharpe) : 'accruing…'}
                  sub={fwd.n ? `${fwd.n} days · ${pct(fwd.ann_return)}` : `since ${data.inception ?? '—'}`}
                  subColor="neutral" />
      </div>

      {/* Equity curve */}
      <SectionLabel>Equity Curve (cumulative, paper)</SectionLabel>
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-4 dash-card">
        {equityChart && (
          <Chart kind="area" data={equityChart} height={300}
                 options={{ scales: { x: { ticks: { maxTicksLimit: 8 } }, y: { title: { display: true, text: 'Equity (×)' } } } }} />
        )}
      </div>

      {/* Positions */}
      <SectionLabel>Current Book — Market-Neutral (Long low-funding · Short high-funding)</SectionLabel>
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 md:gap-4">
        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-4 dash-card">
          <div className="text-[11px] uppercase tracking-wider text-green-400 font-semibold mb-2">Longs · {longs.length}</div>
          <DataTable columns={holdingCols} data={longs} density="compact" emptyMessage="No long positions" />
        </div>
        <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-4 dash-card">
          <div className="text-[11px] uppercase tracking-wider text-red-400 font-semibold mb-2">Shorts · {shorts.length}</div>
          <DataTable columns={holdingCols} data={shorts} density="compact" emptyMessage="No short positions" />
        </div>
      </div>

      {/* Recent trades / rebalances */}
      <SectionLabel>Recent Rebalances (weekly)</SectionLabel>
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-3 md:p-4 dash-card">
        <DataTable columns={rebalCols} data={data.positions?.recent_rebalances ?? []} density="compact" emptyMessage="No rebalances yet" />
      </div>

      {/* Disclaimer */}
      <div className="text-[11px] text-[var(--color-text-muted)] italic px-1">{data.disclaimer}</div>
    </div>
  )
}
