import { useLiveState } from '../../api/queries'
import { useForgeState } from '../../api/forge-queries'
import { fmtRelativeTime } from '../../lib/format'
import { HudPanel, StreamDivider } from '../ui/hud'
import { GlyphCheck, GlyphX, GlyphFeed } from '../ui/glyphs'
import type { CycleStatus } from '../../api/forge-types'

const CYCLE_COLOR: Record<CycleStatus, string> = {
  pass: 'var(--mc-forge-hot)',
  near_miss: 'var(--color-warning)',
  fail: 'var(--color-text-muted)',
  error: 'var(--color-negative)',
}

function CycleIcon({ status }: { status: CycleStatus }) {
  const color = CYCLE_COLOR[status]
  if (status === 'pass') return <span style={{ color }} className="font-bold">★</span>
  if (status === 'error') return <GlyphX size={11} className="text-[var(--color-negative)]" />
  if (status === 'near_miss') return <span style={{ color }}>◐</span>
  return <span style={{ color }}>·</span>
}

/** Two-wire activity feed: latest forge cycles | latest live daily results. */
export function ActivityFeed() {
  const { data: forge } = useForgeState()
  const { data: live } = useLiveState()
  const cycles = (forge?.cycles ?? []).slice(0, 5)
  const results = live?.daily?.results ?? []

  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <HudPanel
        title={
          <span className="flex items-center gap-1.5" data-section="forge">
            <GlyphFeed size={12} /> Forge Wire
          </span>
        }
        bodyClassName="space-y-0.5"
      >
        <div data-section="forge">
          <StreamDivider className="mb-2" />
          {cycles.length === 0 ? (
            <Empty label="no forge cycles yet" />
          ) : (
            <ul className="stagger space-y-1.5">
              {cycles.map((c, i) => (
                <li key={c.id ?? i} className="animate-in flex items-center gap-2 text-xs min-w-0">
                  <CycleIcon status={c.status} />
                  <span className="truncate flex-1" title={c.title}>{c.title}</span>
                  {c.metrics?.holdout_sharpe != null && (
                    <span className="font-mono text-[10px] text-[var(--color-text-muted)] shrink-0">
                      hs {c.metrics.holdout_sharpe.toFixed(2)}
                    </span>
                  )}
                  <span className="font-mono text-[10px] text-[var(--color-text-muted)] shrink-0">
                    {c.ts ? fmtRelativeTime(c.ts) : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </HudPanel>

      <HudPanel
        title={
          <span className="flex items-center gap-1.5" data-section="live">
            <GlyphFeed size={12} /> Live Wire
          </span>
        }
        bodyClassName="space-y-0.5"
      >
        <div data-section="live">
          <StreamDivider className="mb-2" />
          {results.length === 0 ? (
            <Empty label="no daily run recorded yet" />
          ) : (
            <ul className="stagger space-y-1.5">
              {results.map((r, i) => (
                <li key={`${r.name}-${i}`} className="animate-in flex items-center gap-2 text-xs min-w-0">
                  {r.blocked ? (
                    <GlyphX size={11} className="text-[var(--color-negative)]" />
                  ) : r.error ? (
                    <GlyphX size={11} className="text-[var(--color-warning)]" />
                  ) : (
                    <GlyphCheck size={11} className="text-[var(--color-positive)]" />
                  )}
                  <span className="truncate flex-1 font-medium">{r.name}</span>
                  <span className="font-mono text-[10px] text-[var(--color-text-muted)] shrink-0">
                    {r.state}/{r.broker} · {r.n_orders} orders · {r.executed} exec
                    {r.track_status ? ` · ${r.track_status}` : ''}
                  </span>
                </li>
              ))}
              {live?.daily?.date && (
                <li className="text-[10px] font-mono text-[var(--color-text-muted)] pt-1">
                  cycle {live.daily.date} ({live.daily.mode})
                </li>
              )}
            </ul>
          )}
        </div>
      </HudPanel>
    </div>
  )
}

function Empty({ label }: { label: string }) {
  return <div className="text-[11px] font-mono tracking-[0.1em] text-[var(--color-text-muted)] py-2">{label.toUpperCase()}</div>
}
