/** Variant B — "Mission Control": telemetry gauges, a T-minus countdown, a live
 *  green-on-black event feed, and the FDR discipline-ratchet line chart. */
import { useMemo } from 'react'
import type { ForgeState } from '../../api/forge-types'
import type { ChartData, ChartOptions } from 'chart.js'
import { Chart } from '../shared/Chart'
import { fmtRelativeTime } from '../../lib/format'
import { C, useCountdown, pad, Card, RadialGauge, statusLabel } from './shared'

export function VariantMissionControl({ state }: { state: ForgeState }) {
  const { status, counts, fdr, cycles } = state
  const cd = useCountdown(status.next_run_ms)
  const passRate = counts.cycles ? counts.passes / counts.cycles : 0

  const chartData: ChartData<'line'> = useMemo(() => ({
    labels: fdr.history.map((_, i) => `f${i + 1}`),
    datasets: [{
      label: 'promotion bar (DSR)',
      data: fdr.history,
      borderColor: C.cyan,
      backgroundColor: 'rgba(34,211,238,0.12)',
      fill: true, tension: 0.3, pointRadius: 3, pointBackgroundColor: C.cyan, borderWidth: 2,
    }],
  }), [fdr.history])

  const chartOpts: ChartOptions<'line'> = useMemo(() => ({
    scales: {
      y: { min: 0.85, max: 1, ticks: { color: '#a1a1aa', font: { size: 10 } }, grid: { color: '#2a2a2e' } },
      x: { ticks: { color: '#a1a1aa', font: { size: 10 } }, grid: { display: false } },
    },
    plugins: { legend: { display: false } },
  }), [])

  const feed = [
    ...cycles.map((c) => ({
      ts: c.ts, txt: `${statusLabel(c.status, c.tier)} ${c.title}`,
      col: c.status === 'pass' ? C.gold : c.status === 'error' ? C.red : C.green,
    })),
  ]

  return (
    <div className="space-y-4">
      {/* ── Top row: countdown + status + gauges ── */}
      <div className="grid lg:grid-cols-4 gap-4">
        <Card className="p-5 lg:col-span-1 relative overflow-hidden">
          <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)]">T‑minus next run</div>
          <div className="mt-2 text-4xl font-bold tabular-nums font-mono" style={{ color: C.cyan }}>
            {status.enabled ? `${pad(cd.h)}:${pad(cd.m)}:${pad(cd.s)}` : 'OFFLINE'}
          </div>
          <div className="mt-1 text-[11px] text-[var(--color-text-muted)]">{status.next_run_str || '—'}</div>
          <div className="mt-4 flex items-center gap-2">
            <span className="inline-block w-2.5 h-2.5 rounded-full forge-blink" style={{ background: status.running ? C.green : C.red }} />
            <span className="text-sm font-semibold" style={{ color: status.running ? C.green : C.red }}>
              {status.running ? 'SYSTEMS NOMINAL' : 'LOOP HALTED'}
            </span>
          </div>
          <div className="text-[11px] text-[var(--color-text-muted)] mt-1">timer {status.enabled ? 'enabled' : 'disabled'} · last cycle {fmtRelativeTime(status.last_cycle_ts)}</div>
        </Card>
        <Card className="p-4 lg:col-span-3">
          <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)] mb-2">Telemetry</div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <RadialGauge value={passRate} label="full-gate pass rate" color={C.gold} sub={`${counts.passes}/${counts.cycles}`} />
            <RadialGauge value={counts.candidates} max={14} label="candidate queue depth" color={C.cyan} sub={String(counts.candidates)} />
            <RadialGauge value={(fdr.bar - 0.85) / 0.15} label={`FDR bar ${fdr.bar.toFixed(3)}`} color={C.indigo} sub={fdr.bar.toFixed(2)} />
            <RadialGauge value={fdr.n_families} max={24} label="families in FDR memory" color={C.ember} sub={String(fdr.n_families)} />
          </div>
        </Card>
      </div>

      {/* ── Bottom row: feed + ratchet chart ── */}
      <div className="grid lg:grid-cols-2 gap-4">
        <Card className="p-0 overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2 border-b border-[var(--color-border)]">
            <span className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)]">event feed</span>
            <span className="flex items-center gap-1.5 text-[10px]" style={{ color: C.red }}>
              <span className="forge-blink">●</span> REC
            </span>
          </div>
          <div className="p-3 font-mono text-[11px] leading-relaxed max-h-[280px] overflow-y-auto bg-[#0a0a0c]">
            {feed.length === 0 ? (
              <div className="text-[var(--color-text-muted)]">// awaiting first cycle…</div>
            ) : feed.map((f, i) => (
              <div key={i} className="flex gap-2">
                <span className="text-[var(--color-text-muted)] shrink-0">{(f.ts || '').slice(5, 16).replace('T', ' ')}</span>
                <span className="truncate" style={{ color: f.col }}>{f.txt}</span>
              </div>
            ))}
            <div className="text-[var(--color-text-muted)]">{'>'}<span className="forge-blink">_</span></div>
          </div>
        </Card>
        <Card className="p-4">
          <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)] mb-2">Discipline ratchet — promotion bar climbs per family</div>
          <Chart kind="line" data={chartData} options={chartOpts as ChartOptions<'line' | 'bar' | 'doughnut'>} height={236} />
        </Card>
      </div>
    </div>
  )
}
