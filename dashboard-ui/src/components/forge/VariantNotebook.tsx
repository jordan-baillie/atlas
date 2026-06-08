/** Variant D — "Lab Notebook": a notebook that fills itself. A knowledge-
 *  compounding stat band, the discipline ratchet sparkline, a feed of lab cards
 *  with rubber-stamp verdicts, and an on-deck column of scout candidates. */
import type { ForgeState } from '../../api/forge-types'
import { Sparkline } from '../shared/Sparkline'
import { fmtRelativeTime } from '../../lib/format'
import { C, Card, statusLabel } from './shared'

function Stamp({ status, tier }: { status: 'pass' | 'fail' | 'error'; tier: string | null }) {
  const col = status === 'pass' ? C.gold : status === 'error' ? C.red : C.iron
  return (
    <div className="forge-stamp absolute top-3 right-3 px-2 py-1 rounded border-2 font-black tracking-widest text-xs select-none"
      style={{ color: col, borderColor: col, opacity: 0.8 }}>
      {statusLabel(status, tier)}
    </div>
  )
}

export function VariantNotebook({ state }: { state: ForgeState }) {
  const { counts, cycles, candidates, fdr } = state
  const stats = [
    { label: 'experiments', value: counts.experiments, icon: '🧪' },
    { label: 'FDR families', value: counts.families, icon: '🧬' },
    { label: 'candidates', value: counts.candidates, icon: '💡' },
    { label: 'wiki pages', value: counts.wiki_pages, icon: '📚' },
  ]

  return (
    <div className="space-y-4">
      {/* Knowledge band */}
      <Card className="p-5">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div className="grid grid-cols-4 gap-4 flex-1">
            {stats.map((s) => (
              <div key={s.label}>
                <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] flex items-center gap-1">{s.icon} {s.label}</div>
                <div className="text-3xl font-bold tabular-nums text-[var(--color-text)]">{s.value}</div>
              </div>
            ))}
          </div>
          <div className="md:w-64 shrink-0">
            <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] mb-1">discipline ratchet (DSR bar)</div>
            <Sparkline data={fdr.history.length > 1 ? fdr.history : [0.9, fdr.bar]} color={C.indigo} height={40} strokeWidth={2} />
            <div className="text-[11px] text-[var(--color-text-muted)] mt-1">now <span style={{ color: C.indigo }}>{fdr.bar.toFixed(3)}</span> — rises as knowledge compounds</div>
          </div>
        </div>
      </Card>

      <div className="grid lg:grid-cols-3 gap-4">
        {/* Lab cards feed */}
        <div className="lg:col-span-2 space-y-3">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] px-1">📓 Lab notebook — what the agent tried</div>
          {cycles.length === 0 ? (
            <Card className="p-6 text-center text-sm text-[var(--color-text-muted)]">First entry writes itself tonight.</Card>
          ) : cycles.map((c, i) => (
            <Card key={i} className="p-4 relative overflow-hidden">
              <Stamp status={c.status} tier={c.tier} />
              <div className="text-[10px] text-[var(--color-text-muted)]">{fmtRelativeTime(c.ts)}</div>
              <div className="text-sm font-semibold text-[var(--color-text)] mt-0.5 pr-16 leading-snug">{c.title}</div>
              {c.premium && <div className="text-[11px] text-[var(--color-text-muted)] mt-1.5 line-clamp-2"><span className="text-[var(--color-text)]">premium:</span> {c.premium}</div>}
              {c.market && <div className="text-[11px] text-[var(--color-text-muted)] mt-0.5 line-clamp-1"><span className="text-[var(--color-text)]">market:</span> {c.market}</div>}
              <div className="mt-2.5 flex items-center gap-1.5 flex-wrap">
                <Chip k="rails" v={c.ran ? 'ran' : 'error'} ok={c.ran} />
                <Chip k="tier" v={c.tier || 'FAIL'} ok={!!c.tier && !/fail/i.test(c.tier)} />
                <Chip k="holdout" v={c.holdout_pass === null ? 'n/a' : c.holdout_pass ? 'pass' : 'untouched'} ok={c.holdout_pass === true} />
                {c.dsr != null && <Chip k="dsr" v={c.dsr.toFixed(2)} ok={c.passed_all} />}
              </div>
            </Card>
          ))}
        </div>

        {/* On-deck candidates */}
        <div className="space-y-3">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] px-1">🔭 On deck — scout queue</div>
          {candidates.length === 0 ? (
            <Card className="p-6 text-center text-sm text-[var(--color-text-muted)]">Queue empty.</Card>
          ) : candidates.map((c, i) => (
            <Card key={i} className="p-3.5">
              <div className="flex items-start justify-between gap-2">
                <div className="text-xs font-semibold text-[var(--color-text)] leading-snug">{c.title}</div>
                <span className="shrink-0 px-1.5 py-0.5 rounded text-[9px] font-bold"
                  style={{ background: c.free ? 'rgba(34,197,94,0.15)' : 'rgba(245,158,11,0.15)', color: c.free ? C.green : C.ember }}>
                  {c.free ? 'FREE' : 'DATA?'}
                </span>
              </div>
              <div className="text-[10px] text-[var(--color-text-muted)] mt-1 italic">{c.tags}</div>
              <div className="text-[11px] text-[var(--color-text-muted)] mt-1.5 line-clamp-3">{c.summary}</div>
            </Card>
          ))}
        </div>
      </div>
    </div>
  )
}

function Chip({ k, v, ok }: { k: string; v: string; ok: boolean }) {
  return (
    <span className="px-1.5 py-0.5 rounded text-[10px] tabular-nums"
      style={{ background: 'var(--color-surface-alt)', color: ok ? C.green : 'var(--color-text-muted)' }}>
      <span className="opacity-60">{k}</span> {v}
    </span>
  )
}
