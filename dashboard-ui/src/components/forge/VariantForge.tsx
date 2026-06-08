/** Variant A — "The Forge": the loop rendered as a live assembly line of forge
 *  stations, with a traveling ingot, ember particles, a status hero + countdown,
 *  and a heartbeat strip of recent cycles. */
import type { ForgeState } from '../../api/forge-types'
import { fmtRelativeTime } from '../../lib/format'
import { C, STAGE_ICON, useCountdown, pad, Card, statusColor, statusLabel } from './shared'

function Embers({ active }: { active: boolean }) {
  if (!active) return null
  return (
    <div className="absolute inset-x-0 bottom-6 h-12 pointer-events-none overflow-visible">
      {[0, 1, 2, 3, 4].map((i) => (
        <span
          key={i}
          className="forge-ember absolute bottom-0 rounded-full"
          style={{
            left: `${15 + i * 17}%`,
            width: 3 + (i % 2), height: 3 + (i % 2),
            background: i % 2 ? C.gold : C.hot,
            ['--ex' as string]: `${(i % 2 ? 1 : -1) * (4 + i * 2)}px`,
            animationDelay: `${i * 0.45}s`,
          }}
        />
      ))}
    </div>
  )
}

export function VariantForge({ state }: { state: ForgeState }) {
  const { status, pipeline, cycles, fdr, counts } = state
  const cd = useCountdown(status.next_run_ms)
  const running = status.running

  return (
    <div className="space-y-4">
      {/* ── Hero status ── */}
      <Card glow={running} className="p-5 relative overflow-hidden">
        <div className="absolute inset-0 opacity-[0.06] pointer-events-none"
          style={{ background: `radial-gradient(120% 100% at 50% 0%, ${C.ember}, transparent 60%)` }} />
        <div className="relative flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="flex items-center gap-4">
            <div className="text-5xl forge-glow">{running ? '🔥' : '🧊'}</div>
            <div>
              <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)]">Hephaestus forge</div>
              <div className="text-2xl font-bold flex items-center gap-2" style={{ color: running ? C.ember : C.iron }}>
                {running ? 'RUNNING' : 'HALTED'}
                <span className="inline-block w-2.5 h-2.5 rounded-full forge-blink" style={{ background: running ? C.green : C.iron }} />
              </div>
              <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5">
                last cycle {fmtRelativeTime(status.last_cycle_ts)} · {counts.cycles} run all-time
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)]">next firing</div>
            <div className="text-3xl font-bold tabular-nums" style={{ color: C.gold }}>
              {status.enabled ? `${pad(cd.h)}:${pad(cd.m)}:${pad(cd.s)}` : '— disabled —'}
            </div>
            <div className="text-[11px] text-[var(--color-text-muted)]">{status.next_run_str || 'nightly 03:30'}</div>
          </div>
        </div>
      </Card>

      {/* ── Pipeline ── */}
      <Card className="p-5">
        <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] mb-4">The forge line</div>
        <div className="relative">
          {/* moving ingot track */}
          <div className="absolute top-[34px] left-[5%] right-[5%] h-[2px] bg-[var(--color-surface-alt)] rounded">
            {running && <span className="forge-travel absolute -top-[3px] w-2 h-2 rounded-full"
              style={{ background: C.gold, boxShadow: `0 0 10px 3px ${C.ember}` }} />}
          </div>
          <div className="relative grid grid-cols-3 md:grid-cols-6 gap-3">
            {pipeline.map((s, i) => {
              const isAlert = s.key === 'alert'
              const hot = isAlert && s.count > 0
              return (
                <div key={s.key} className="relative flex flex-col items-center text-center">
                  <div
                    className={`relative w-16 h-16 rounded-2xl flex items-center justify-center text-2xl border ${running ? 'forge-glow' : ''}`}
                    style={{
                      background: hot ? 'rgba(251,191,36,0.12)' : 'var(--color-surface-alt)',
                      borderColor: hot ? C.gold : 'var(--color-border)',
                      animationDelay: `${i * 0.3}s`,
                    }}
                  >
                    {STAGE_ICON[s.key]}
                    <Embers active={running} />
                  </div>
                  <div className="mt-2 text-2xl font-bold tabular-nums" style={{ color: hot ? C.gold : 'var(--color-text)' }}>{s.count}</div>
                  <div className="text-xs font-medium text-[var(--color-text)]">{s.label}</div>
                  <div className="text-[10px] text-[var(--color-text-muted)] leading-tight">{s.sub}</div>
                </div>
              )
            })}
          </div>
        </div>
      </Card>

      {/* ── Heartbeat + discipline bar ── */}
      <div className="grid md:grid-cols-3 gap-4">
        <Card className="p-5 md:col-span-2">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] mb-3">Recent cycles — the heartbeat</div>
          {cycles.length === 0 ? (
            <div className="text-sm text-[var(--color-text-muted)] py-6 text-center">No cycles yet — first firing tonight.</div>
          ) : (
            <div className="flex flex-wrap gap-2">
              {cycles.map((c, i) => (
                <div key={i} className="group relative">
                  <div
                    className="w-7 h-7 rounded-full flex items-center justify-center text-[9px] font-bold cursor-default border"
                    style={{
                      background: c.status === 'pass' ? 'rgba(251,191,36,0.18)' : 'var(--color-surface-alt)',
                      borderColor: statusColor(c.status), color: statusColor(c.status),
                    }}
                  >
                    {c.status === 'pass' ? '★' : c.status === 'error' ? '!' : '·'}
                  </div>
                  <div className="absolute z-20 bottom-9 left-1/2 -translate-x-1/2 hidden group-hover:block w-56 p-2.5 rounded-lg bg-[var(--color-surface-alt)] border border-[var(--color-border)] shadow-xl text-left">
                    <div className="text-[11px] font-semibold text-[var(--color-text)] leading-snug">{c.title}</div>
                    <div className="text-[10px] text-[var(--color-text-muted)] mt-1">{statusLabel(c.status, c.tier)} · {fmtRelativeTime(c.ts)}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="mt-4 flex items-center gap-4 text-[11px] text-[var(--color-text-muted)]">
            <span className="flex items-center gap-1"><span style={{ color: C.gold }}>★</span> full pass</span>
            <span className="flex items-center gap-1"><span style={{ color: C.iron }}>·</span> honest fail</span>
            <span className="flex items-center gap-1"><span style={{ color: C.red }}>!</span> error</span>
          </div>
        </Card>
        <Card className="p-5 flex flex-col justify-center">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] mb-2">Discipline ratchet</div>
          <div className="text-4xl font-bold tabular-nums" style={{ color: C.indigo }}>{fdr.bar.toFixed(3)}</div>
          <div className="text-[11px] text-[var(--color-text-muted)]">FDR promotion bar — rises with every family tested</div>
          <div className="mt-3 h-2 w-full rounded-full bg-[var(--color-surface-alt)] overflow-hidden">
            <div className="h-full rounded-full" style={{ width: `${(fdr.bar - 0.85) / 0.15 * 100}%`, background: `linear-gradient(90deg, ${C.indigo}, ${C.gold})` }} />
          </div>
          <div className="text-[11px] text-[var(--color-text-muted)] mt-2">{fdr.n_families} families in FDR memory</div>
        </Card>
      </div>
    </div>
  )
}
