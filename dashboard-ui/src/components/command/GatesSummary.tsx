import { useLiveState } from '../../api/queries'
import { HudPanel, GateStatusPill } from '../ui/hud'
import { GlyphGate } from '../ui/glyphs'

/** Compact go-live gates row for the Command tab (rollup across strategies). */
export function GatesSummary() {
  const { data } = useLiveState()
  const gates = data?.gates
  const per = gates?.per_strategy ?? {}
  const first = Object.values(per)[0]

  return (
    <HudPanel
      title={
        <span className="flex items-center gap-1.5">
          <GlyphGate size={12} /> Go-Live Gates
        </span>
      }
      right={
        gates?.overall && (
          <GateStatusPill label="OVERALL" pass={gates.overall.pass} />
        )
      }
    >
      {!gates || !first ? (
        <div className="text-[11px] font-mono tracking-[0.14em] text-[var(--color-text-muted)] py-1">
          AWAITING DATA — gates accrue as the forward-paper book trades
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
          <GateRow
            pill={<GateStatusPill label="G6 SLIPPAGE" pass={first.slippage?.pass} />}
            value={
              first.slippage?.median_bps != null
                ? `${first.slippage.median_bps.toFixed(1)} bps median · bar ${first.slippage.bar_bps ?? 16} · n=${first.slippage.n_fills}`
                : 'no fills yet'
            }
          />
          <GateRow
            pill={<GateStatusPill label="G7 BROKER" pass={first.broker_errors?.pass} />}
            value={
              first.broker_errors?.error_rate_pct != null
                ? `${first.broker_errors.error_rate_pct}% errors · bar <${first.broker_errors.bar_pct ?? 1}% · n=${first.broker_errors.n_orders}`
                : 'no orders yet'
            }
          />
          <GateRow
            pill={<GateStatusPill label="TRACK" pass={first.track?.pass} />}
            value={
              first.track?.status
                ? `${first.track.status} · ${first.track.n_obs ?? 0} obs${first.track.realized_sharpe != null ? ` · sharpe ${first.track.realized_sharpe.toFixed(2)} vs ${first.track.expected_sharpe?.toFixed(2) ?? '—'}` : ''}`
                : 'no expectation set'
            }
          />
        </div>
      )}
    </HudPanel>
  )
}

function GateRow({ pill, value }: { pill: React.ReactNode; value: string }) {
  return (
    <div className="flex items-center gap-2">
      {pill}
      <span className="text-[11px] font-mono text-[var(--color-text-muted)]">{value}</span>
    </div>
  )
}
