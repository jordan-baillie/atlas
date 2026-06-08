/** The forge line — six stations of the autonomous loop, each with live stats. */
import type { ForgeStage } from '../../api/forge-types'
import { C, Card } from './shared'

function Embers({ active }: { active: boolean }) {
  if (!active) return null
  return (
    <div className="absolute inset-x-0 -bottom-1 h-10 pointer-events-none overflow-visible">
      {[0, 1, 2].map((i) => (
        <span key={i} className="forge-ember absolute bottom-0 rounded-full"
          style={{
            left: `${28 + i * 22}%`, width: 3, height: 3, background: i % 2 ? C.gold : C.hot,
            ['--ex' as string]: `${(i % 2 ? 1 : -1) * (4 + i * 2)}px`, animationDelay: `${i * 0.5}s`,
          }} />
      ))}
    </div>
  )
}

export function ForgeLine({ pipeline, running }: { pipeline: ForgeStage[]; running: boolean }) {
  return (
    <Card className="p-5">
      <div className="flex items-center justify-between mb-4">
        <div className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)]">The forge line</div>
        <div className="text-[10px] text-[var(--color-text-muted)]">scout → propose → codegen → rails → record → alert</div>
      </div>

      <div className="relative">
        {/* connecting track + traveling ingot (lg only, behind the icon row) */}
        <div className="hidden lg:block absolute top-8 left-[8%] right-[8%] h-[2px] bg-[var(--color-surface-alt)] rounded">
          {running && (
            <span className="forge-travel absolute -top-[3px] w-2 h-2 rounded-full"
              style={{ background: C.gold, boxShadow: `0 0 10px 3px ${C.ember}` }} />
          )}
        </div>

        <div className="relative grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          {pipeline.map((s, i) => {
            const hot = s.accent && s.count > 0
            return (
              <div key={s.key} className="flex flex-col items-center text-center">
                <div className="relative w-16 h-16 rounded-2xl flex items-center justify-center text-2xl border"
                  style={{
                    background: hot ? 'rgba(251,191,36,0.12)' : 'var(--color-surface-alt)',
                    borderColor: hot ? C.gold : 'var(--color-border)',
                  }}>
                  <span className={running ? 'forge-glow' : ''} style={{ animationDelay: `${i * 0.3}s` }}>{s.icon}</span>
                  <Embers active={running} />
                </div>
                <div className="mt-2 text-2xl font-bold tabular-nums" style={{ color: hot ? C.gold : 'var(--color-text)' }}>{s.count}</div>
                <div className="text-xs font-semibold text-[var(--color-text)]">{s.label}</div>

                {/* per-stage stats */}
                <div className="mt-2 w-full space-y-1">
                  {s.stats.map((st) => (
                    <div key={st.label} className="flex items-center justify-between gap-1 text-[10px] px-2 py-0.5 rounded bg-[var(--color-surface-alt)]/60">
                      <span className="text-[var(--color-text-muted)] truncate">{st.label}</span>
                      <span className="font-semibold tabular-nums text-[var(--color-text)]">{st.value}</span>
                    </div>
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </Card>
  )
}
