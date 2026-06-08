/** Variant C — "The Gauntlet": hypotheses enter the top and fall through the
 *  rails' gates, narrowing sharply. A rejects gallery celebrates the negative
 *  knowledge; a gold burst is reserved for a real full-gate pass. */
import type { ForgeState } from '../../api/forge-types'
import { fmtRelativeTime } from '../../lib/format'
import { C, Card } from './shared'

export function VariantGauntlet({ state }: { state: ForgeState }) {
  const { counts, cycles, fdr } = state
  const coded = counts.ran
  const tested = counts.cycles
  const clearedTier = cycles.filter((c) => c.tier && !/fail/i.test(c.tier)).length
  const clearedHoldout = cycles.filter((c) => c.holdout_pass === true).length
  const passed = counts.passes

  const gates = [
    { label: 'Candidates queued', n: counts.candidates, col: C.cyan },
    { label: 'Coded by the agent', n: coded, col: '#38bdf8' },
    { label: 'Ran the rails', n: tested, col: C.indigo },
    { label: 'Cleared CPCV / DSR / PBO', n: clearedTier, col: '#a78bfa' },
    { label: 'Survived write-once holdout', n: clearedHoldout, col: C.ember },
    { label: '🔔 Full-gate PASS', n: passed, col: C.gold },
  ]
  const top = Math.max(1, gates[0].n)
  const rejects = cycles.filter((c) => c.status !== 'pass')

  return (
    <div className="space-y-4 relative">
      {passed > 0 && (
        <div className="absolute inset-0 pointer-events-none overflow-hidden z-10">
          {Array.from({ length: 24 }).map((_, i) => (
            <span key={i} className="forge-ember absolute rounded-full"
              style={{ left: `${(i * 4.1) % 100}%`, bottom: 0, width: 5, height: 5, background: i % 2 ? C.gold : C.ember, animationDelay: `${(i % 6) * 0.3}s`, animationDuration: '3s' }} />
          ))}
        </div>
      )}

      <Card className="p-5">
        <div className="flex items-center justify-between mb-1">
          <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)]">The Gauntlet — survival through the rails</div>
          <div className="text-[11px] text-[var(--color-text-muted)]">FDR bar <span style={{ color: C.indigo }}>{fdr.bar.toFixed(3)}</span></div>
        </div>
        <div className="text-[11px] text-[var(--color-text-muted)] mb-4">Almost nothing survives — that’s the rails working. Each killed hypothesis is capital saved.</div>

        <div className="flex flex-col items-center gap-1.5">
          {gates.map((g, i) => {
            const w = 30 + (g.n / top) * 70 // % width, min 30
            const prev = i > 0 ? gates[i - 1].n : g.n
            const rejected = Math.max(0, prev - g.n)
            const isPass = i === gates.length - 1
            return (
              <div key={i} className="w-full flex items-center justify-center gap-3">
                <div className="w-28 text-right text-[10px] text-[var(--color-text-muted)] hidden sm:block">
                  {i > 0 && rejected > 0 && <span style={{ color: C.iron }}>− {rejected} cut</span>}
                </div>
                <div
                  className={`relative h-12 rounded-lg flex items-center justify-between px-4 transition-all ${isPass && g.n > 0 ? 'forge-pulse' : ''}`}
                  style={{
                    width: `${w}%`,
                    background: `linear-gradient(90deg, ${g.col}22, ${g.col}0d)`,
                    border: `1px solid ${g.col}66`,
                  }}
                >
                  <span className="text-xs font-medium text-[var(--color-text)] truncate">{g.label}</span>
                  <span className="text-lg font-bold tabular-nums ml-3" style={{ color: g.col }}>{g.n}</span>
                </div>
                <div className="w-28 hidden sm:block" />
              </div>
            )
          })}
        </div>
      </Card>

      {/* Rejects gallery */}
      <Card className="p-5">
        <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] mb-3">⚰️ Rejects gallery — negative knowledge banked</div>
        {rejects.length === 0 ? (
          <div className="text-sm text-[var(--color-text-muted)] py-4 text-center">No rejects yet.</div>
        ) : (
          <div className="grid sm:grid-cols-2 gap-2">
            {rejects.map((c, i) => (
              <div key={i} className="flex items-start gap-3 p-3 rounded-lg bg-[var(--color-surface-alt)] border border-[var(--color-border)]">
                <div className="text-lg shrink-0">{c.status === 'error' ? '💥' : '🪦'}</div>
                <div className="min-w-0">
                  <div className="text-xs font-medium text-[var(--color-text)] leading-snug line-clamp-2">{c.title}</div>
                  <div className="text-[10px] mt-1 flex items-center gap-2">
                    <span className="px-1.5 py-0.5 rounded font-bold" style={{ background: 'rgba(113,113,122,0.15)', color: C.iron }}>
                      cause: {c.tier || (c.status === 'error' ? 'build error' : 'tier FAIL')}
                    </span>
                    <span className="text-[var(--color-text-muted)]">{fmtRelativeTime(c.ts)}</span>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
