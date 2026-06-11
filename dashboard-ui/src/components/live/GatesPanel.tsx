import type { GoLiveGates, LiveState } from '../../api/queries'
import { HudPanel, GaugeBar, GateStatusPill } from '../ui/hud'
import { GlyphGate } from '../ui/glyphs'

/** The Live tab centerpiece: G6 / G7 / track gauges — the evidence gating real capital. */
export function GatesPanel({ gates }: { gates: LiveState['gates'] }) {
  const per = gates?.per_strategy ?? {}
  const names = Object.keys(per)

  if (!gates || names.length === 0) {
    return (
      <HudPanel
        title={<span className="flex items-center gap-1.5"><GlyphGate size={12} /> Go-Live Gates</span>}
        brackets
      >
        <div className="text-[11px] font-mono tracking-[0.14em] text-[var(--color-text-muted)] py-3 text-center">
          AWAITING DATA — gate evidence accrues as the forward-paper book trades
        </div>
      </HudPanel>
    )
  }

  // headline gauges from the first strategy (single-strategy era); per-strategy rows below
  const first = per[names[0]]

  return (
    <HudPanel
      title={<span className="flex items-center gap-1.5"><GlyphGate size={12} /> Go-Live Gates</span>}
      right={gates.overall && <GateStatusPill label="OVERALL" pass={gates.overall.pass} />}
      brackets
    >
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <GateCard
          name="G6 · Slippage"
          desc={`median fill slippage over ${first.slippage?.lookback_days ?? 60}d · n=${first.slippage?.n_fills ?? 0}`}
          pass={first.slippage?.pass}
        >
          <GaugeBar
            value={first.slippage?.median_bps}
            bar={first.slippage?.bar_bps ?? 16}
            pass={first.slippage?.pass}
            fmt={(v) => `${v.toFixed(1)} bps`}
          />
          {first.slippage?.worst_bps != null && (
            <div className="text-[10px] font-mono text-[var(--color-text-muted)] mt-1.5">
              p75 {first.slippage.p75_bps?.toFixed(1) ?? '—'} · worst {first.slippage.worst_bps.toFixed(1)} bps
            </div>
          )}
        </GateCard>

        <GateCard
          name="G7 · Broker Errors"
          desc={`rejected orders over ${first.broker_errors?.n_orders ?? 0} placed`}
          pass={first.broker_errors?.pass}
        >
          <GaugeBar
            value={first.broker_errors?.error_rate_pct}
            bar={first.broker_errors?.bar_pct ?? 1}
            max={Math.max(2, (first.broker_errors?.error_rate_pct ?? 0) * 1.2)}
            pass={first.broker_errors?.pass}
            fmt={(v) => `${v.toFixed(2)}%`}
          />
          {(first.broker_errors?.n_unmatched ?? 0) > 0 && (
            <div className="text-[10px] font-mono text-[var(--color-warning)] mt-1.5">
              {first.broker_errors?.n_unmatched} unmatched order results
            </div>
          )}
        </GateCard>

        <GateCard
          name="Track vs Expectation"
          desc={`${first.track?.n_obs ?? 0} observed days`}
          pass={first.track?.pass}
        >
          <div className="display-num text-lg uppercase" style={{ color: trackColor(first.track?.status) }}>
            {first.track?.status ?? '—'}
          </div>
          <div className="text-[10px] font-mono text-[var(--color-text-muted)] mt-1">
            sharpe {first.track?.realized_sharpe?.toFixed(2) ?? '—'} realized
            {' / '}{first.track?.expected_sharpe?.toFixed(2) ?? '—'} modeled
            {first.track?.mean_z != null && ` · z ${first.track.mean_z.toFixed(1)}`}
          </div>
          {(first.track?.reasons?.length ?? 0) > 0 && (
            <div className="text-[10px] font-mono text-[var(--color-warning)] mt-1 truncate" title={first.track?.reasons?.join('; ')}>
              {first.track?.reasons?.[0]}
            </div>
          )}
        </GateCard>
      </div>

      {/* per-strategy breakdown (matters once N > 1) */}
      {names.length > 1 && (
        <div className="mt-4 space-y-1">
          {names.map((n) => (
            <details key={n} className="group">
              <summary className="cursor-pointer text-xs font-mono flex items-center gap-2 py-1.5 px-2 rounded hover:bg-[var(--color-surface-alt)]">
                <span className="text-[var(--color-text-muted)] group-open:rotate-90 transition-transform">▶</span>
                {n}
                <span className="flex-1" />
                <GateStatusPill label="" pass={per[n].pass} />
              </summary>
              <div className="flex flex-wrap gap-2 px-7 pb-2">
                <GateStatusPill label="G6" pass={per[n].slippage?.pass}
                  detail={`median ${per[n].slippage?.median_bps ?? '—'} bps`} />
                <GateStatusPill label="G7" pass={per[n].broker_errors?.pass}
                  detail={`${per[n].broker_errors?.error_rate_pct ?? '—'}% errors`} />
                <GateStatusPill label="TRK" pass={per[n].track?.pass}
                  detail={per[n].track?.status ?? undefined} />
              </div>
            </details>
          ))}
        </div>
      )}
    </HudPanel>
  )
}

function trackColor(status?: string | null): string {
  switch (status) {
    case 'on_track': return 'var(--color-positive)'
    case 'insufficient': return 'var(--color-warning)'
    case 'diverging': return 'var(--color-warning)'
    case 'halt': return 'var(--color-negative)'
    default: return 'var(--color-text-muted)'
  }
}

function GateCard({ name, desc, pass, children }: {
  name: string
  desc: string
  pass: boolean | null | undefined
  children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-surface-alt)]/40 p-3">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-[10.5px] font-semibold uppercase tracking-[0.14em]">{name}</span>
        <GateStatusPill label="" pass={pass} />
      </div>
      {children}
      <div className="text-[9.5px] uppercase tracking-[0.1em] text-[var(--color-text-muted)] mt-2">{desc}</div>
    </div>
  )
}

export type { GoLiveGates }
