/**
 * ForgeTab — live mission-control for the Hephaestus autonomous research loop.
 *
 * Layout: a slim status strip (no countdown — the loop fires nightly at a fixed
 * time), the six-station forge line with per-stage stats, then a list of recent
 * runs that each expand to a full per-run summary (hypothesis + data + verdict).
 */
import { lazy, Suspense, useState } from 'react'
import { useForgeState } from '../../api/forge-queries'
import { fmtRelativeTime } from '../../lib/format'
import { Skeleton } from '../layout/Skeleton'
import { useCelebration } from '../../hooks/useCelebration'
import { AnimatedNumber } from '../ui/AnimatedNumber'
import { C, Card } from './shared'
import { ForgeLine } from './ForgeLine'
import { RunCard } from './RunCard'
import type { ForgeStatus } from '../../api/forge-types'

// The map is a separate heavy-ish view — lazy-load it so the Monitor stays instant.
const ResearchMap = lazy(() => import('./ResearchMap').then((m) => ({ default: m.ResearchMap })))

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

function SubViewSwitch({ view, setView }: { view: 'monitor' | 'map'; setView: (v: 'monitor' | 'map') => void }) {
  return (
    <div className="inline-flex rounded-lg border border-[var(--color-border)] overflow-hidden text-[11px] font-bold tracking-wide">
      {(['monitor', 'map'] as const).map((v) => (
        <button key={v} onClick={() => setView(v)}
          className={`px-3 py-1.5 transition-colors ${view === v
            ? 'bg-[var(--color-surface-alt)] text-[var(--color-text)]'
            : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]'}`}>
          {v === 'monitor' ? '\u{1F525} Monitor' : '\u{1F5FA}\u{FE0F} Map'}
        </button>
      ))}
    </div>
  )
}

export function ForgeTab() {
  const q = useForgeState()
  const [view, setView] = useState<'monitor' | 'map'>('monitor')

  // Celebrations — hooks must run unconditionally (before any early return)
  const latest0 = q.data?.cycles?.[0]
  const passStamp = latest0 && latest0.status === 'pass' ? `${latest0.id ?? ''}|${latest0.ts ?? ''}` : null
  const passParty = useCelebration('forge-pass', passStamp)
  const deployStamp = (q.data?.summary?.deployed ?? 0) > 0 ? (q.data?.summary?.deployed_names ?? []).join(',') : null
  const deployParty = useCelebration('deploy', deployStamp)
  const celebrating = passParty.celebrating || deployParty.celebrating

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
  const running = status.running          // loop enabled (not halted)
  const cycleActive = status.cycle_active === true  // executing right now

  if (view === 'map') {
    return (
      <div className="space-y-4" data-section="forge">
        <div className="flex justify-end"><SubViewSwitch view={view} setView={setView} /></div>
        <Suspense fallback={<Skeleton className="h-96" />}>
          <ResearchMap />
        </Suspense>
      </div>
    )
  }

  return (
    <div className="space-y-4" data-section="forge">
      {/* ── Slim status strip ── */}
      <Card glow={running} brackets className={`px-5 py-3.5 overflow-hidden ${celebrating ? 'mc-celebrate' : ''}`}>
        {celebrating && !passParty.reduced && (
          <>
            <span className="mc-celebrate-beam left-0" aria-hidden />
            {Array.from({ length: 8 }, (_, i) => (
              <span
                key={i}
                aria-hidden
                className="mc-ember-once absolute bottom-1 w-1 h-1 rounded-full"
                style={{
                  left: `${8 + i * 11}%`,
                  background: C.gold,
                  ['--ex' as string]: `${(i % 2 ? 1 : -1) * (6 + i * 2)}px`,
                  animationDelay: `${i * 110}ms`,
                }}
              />
            ))}
          </>
        )}
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className={`text-2xl ${running ? 'forge-glow' : ''}`}>{running ? '🔥' : '🧊'}</span>
            <div>
              <div className="text-sm font-bold text-[var(--color-text)] flex items-center gap-2">
                Crucible Forge
                {celebrating && (
                  <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide mc-stamp"
                    style={{ background: 'rgba(251,191,36,0.18)', color: C.gold }}>
                    ★ {passParty.celebrating ? 'NEW PASS' : 'DEPLOYED'}
                  </span>
                )}
                <span className="px-1.5 py-0.5 rounded text-[10px] font-bold tracking-wide inline-flex items-center gap-1"
                  style={{ background: running ? 'rgba(34,197,94,0.15)' : 'rgba(113,113,122,0.15)', color: running ? C.green : C.iron }}>
                  <span className="w-1.5 h-1.5 rounded-full forge-blink" style={{ background: running ? C.green : C.iron }} />
                  {cycleActive ? 'RUNNING' : running ? 'ARMED' : 'HALTED'}
                </span>
              </div>
              <div className="text-[11px] text-[var(--color-text-muted)]">
                {scheduleText(status)} · last run {fmtRelativeTime(status.last_cycle_ts)}
              </div>
            </div>
          </div>
          <div className="grid grid-cols-3 sm:grid-cols-6 gap-2">
            <Chip label="cycles" value={<AnimatedNumber value={summary.cycles} />} />
            <Chip label="pass rate" value={summary.pass_rate} color={summary.passes > 0 ? C.gold : undefined} />
            <Chip label="near-miss" value={<AnimatedNumber value={summary.near_misses ?? 0} />} color={(summary.near_misses ?? 0) > 0 ? C.ember : undefined} />
            <Chip label="paper-deployed" value={<AnimatedNumber value={summary.deployed ?? 0} />} color={(summary.deployed ?? 0) > 0 ? C.green : undefined} />
            <Chip label="FDR bar" value={<AnimatedNumber value={summary.fdr_bar} format={(v) => v.toFixed(3)} />} color={C.indigo} />
            <Chip label="best holdout" value={summary.best_holdout_sharpe != null ? summary.best_holdout_sharpe.toFixed(2) : '—'} />
          </div>
        </div>
      </Card>

      {/* ── Forge line with per-stage stats ── */}
      <ForgeLine pipeline={pipeline} running={cycleActive} />

      {/* ── Recent runs — click any to expand the full summary ── */}
      <div>
        <div className="flex items-center justify-between mb-2 px-1">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)]">Recent runs — click to expand</div>
          <div className="flex items-center gap-3">
            <div className="text-[11px] text-[var(--color-text-muted)]">{cycles.length} shown</div>
            <SubViewSwitch view={view} setView={setView} />
          </div>
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
