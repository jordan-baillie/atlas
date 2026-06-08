/**
 * ForgeTab — live mission-control for the Hephaestus autonomous research loop.
 *
 * Layout: a slim status strip (no countdown — the loop fires nightly at a fixed
 * time), the six-station forge line with per-stage stats, then a list of recent
 * runs that each expand to a full per-run summary (hypothesis + data + verdict).
 */
import { useForgeState } from '../../api/forge-queries'
import { fmtRelativeTime } from '../../lib/format'
import { Skeleton } from '../layout/Skeleton'
import { C, Card } from './shared'
import { ForgeLine } from './ForgeLine'
import { RunCard } from './RunCard'
import type { ForgeStatus } from '../../api/forge-types'

function Chip({ label, value, color }: { label: string; value: React.ReactNode; color?: string }) {
  return (
    <div className="px-3 py-1.5 rounded-lg bg-[var(--color-surface-alt)] text-center">
      <div className="text-[9px] uppercase tracking-wide text-[var(--color-text-muted)]">{label}</div>
      <div className="text-sm font-bold tabular-nums leading-tight" style={{ color: color || 'var(--color-text)' }}>{value}</div>
    </div>
  )
}

function scheduleText(status: ForgeStatus): string {
  const m = status.next_run_str?.match(/(\d{2}:\d{2}):\d{2}\s*(\w+)?/)
  if (m) return `nightly ${m[1]}${m[2] ? ' ' + m[2] : ''}`
  return 'nightly 03:30'
}

export function ForgeTab() {
  const q = useForgeState()

  if (q.isLoading && !q.data) {
    return <div className="space-y-4"><Skeleton className="h-16" /><Skeleton className="h-56" /><Skeleton className="h-64" /></div>
  }
  if (q.isError || !q.data) {
    return (
      <div className="p-6 text-center text-sm text-[var(--color-negative)] bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl">
        Couldn’t load forge state — <code className="text-xs">/api/forge/state</code>
      </div>
    )
  }

  const { status, summary, pipeline, cycles } = q.data
  const running = status.running

  return (
    <div className="space-y-4">
      {/* ── Slim status strip ── */}
      <Card glow={running} className="px-5 py-3.5">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className={`text-2xl ${running ? 'forge-glow' : ''}`}>{running ? '🔥' : '🧊'}</span>
            <div>
              <div className="text-sm font-bold text-[var(--color-text)] flex items-center gap-2">
                Hephaestus Forge
                <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide inline-flex items-center gap-1"
                  style={{ background: running ? 'rgba(34,197,94,0.15)' : 'rgba(113,113,122,0.15)', color: running ? C.green : C.iron }}>
                  <span className="w-1.5 h-1.5 rounded-full forge-blink" style={{ background: running ? C.green : C.iron }} />
                  {running ? 'RUNNING' : 'HALTED'}
                </span>
              </div>
              <div className="text-[11px] text-[var(--color-text-muted)]">
                {scheduleText(status)} · last run {fmtRelativeTime(status.last_cycle_ts)}
              </div>
            </div>
          </div>
          <div className="grid grid-cols-3 sm:grid-cols-5 gap-2">
            <Chip label="cycles" value={summary.cycles} />
            <Chip label="pass rate" value={summary.pass_rate} color={summary.passes > 0 ? C.gold : undefined} />
            <Chip label="families" value={summary.families} />
            <Chip label="FDR bar" value={summary.fdr_bar.toFixed(3)} color={C.indigo} />
            <Chip label="best holdout" value={summary.best_holdout_sharpe != null ? summary.best_holdout_sharpe.toFixed(2) : '—'} />
          </div>
        </div>
      </Card>

      {/* ── Forge line with per-stage stats ── */}
      <ForgeLine pipeline={pipeline} running={running} />

      {/* ── Recent runs — click any to expand the full summary ── */}
      <div>
        <div className="flex items-center justify-between mb-2 px-1">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)]">Recent runs — click to expand</div>
          <div className="text-[11px] text-[var(--color-text-muted)]">{cycles.length} shown</div>
        </div>
        {cycles.length === 0 ? (
          <Card className="p-6 text-center text-sm text-[var(--color-text-muted)]">No runs yet — the forge fires tonight.</Card>
        ) : (
          <div className="space-y-2">
            {cycles.map((c, i) => <RunCard key={c.id || i} cycle={c} />)}
          </div>
        )}
      </div>
    </div>
  )
}
