/**
 * PaperProgressPanel — displays promotion gate status for PAPER-state strategies.
 *
 * Fetches /api/strategies/paper-progress and renders a compact table showing
 * each strategy's progress toward the promotion bar:
 *   ≥30 days · ≥10 trades · Sharpe ≥0.3 · |Δ vs research| < 0.5
 */

import { usePaperProgress } from '../../api/research-queries'
import { Skeleton } from '../layout/Skeleton'
import type { PaperProgressStrategy } from '../../api/research-types'

// ── Status helpers ────────────────────────────────────────────────────────────

const STATUS_EMOJI: Record<string, string> = {
  ready: '🟢',
  progressing: '🟡',
  failing: '🔴',
  insufficient_data: '⚪',
}

const STATUS_LABEL: Record<string, string> = {
  ready: 'Ready',
  progressing: 'In Progress',
  failing: 'Failing',
  insufficient_data: 'Insufficient Data',
}

const STATUS_COLOR: Record<string, string> = {
  ready: 'var(--color-green)',
  progressing: 'var(--color-amber, #f59e0b)',
  failing: 'var(--color-red)',
  insufficient_data: 'var(--color-text-muted)',
}

function GateCell({ pass, label }: { pass: boolean; label: string }) {
  return (
    <span
      title={label}
      className="inline-block text-xs font-mono px-1.5 py-0.5 rounded"
      style={{
        background: pass ? 'rgba(34,197,94,0.12)' : 'rgba(239,68,68,0.10)',
        color: pass ? 'var(--color-green)' : 'var(--color-red)',
        border: `1px solid ${pass ? 'rgba(34,197,94,0.25)' : 'rgba(239,68,68,0.20)'}`,
      }}
    >
      {pass ? '✓' : '✗'} {label}
    </span>
  )
}

function StrategyRow({ s }: { s: PaperProgressStrategy }) {
  const statusEmoji = STATUS_EMOJI[s.status] ?? '⚪'
  const statusLabel = STATUS_LABEL[s.status] ?? s.status
  const statusColor = STATUS_COLOR[s.status] ?? 'var(--color-text-muted)'

  const fmtNum = (v: number | null | undefined, dp = 2) =>
    v == null ? '—' : v.toFixed(dp)

  const deltaStr =
    s.sharpe_delta == null
      ? '—'
      : `${s.sharpe_delta >= 0 ? '+' : ''}${s.sharpe_delta.toFixed(3)}`

  return (
    <tr className="border-b border-[var(--color-border)] hover:bg-[var(--color-surface-alt)]/40 transition-colors">
      {/* Strategy / Universe */}
      <td className="py-2 px-3 text-sm font-mono whitespace-nowrap">
        <span className="text-[var(--color-text)]">{s.strategy}</span>
        <span className="text-[var(--color-text-muted)] text-xs ml-1">/{s.universe}</span>
      </td>

      {/* Days in PAPER */}
      <td className="py-2 px-3 text-sm font-mono text-right tabular-nums">
        <span style={{ color: s.gates.days_pass ? 'var(--color-green)' : undefined }}>
          {s.days_in_paper}d
        </span>
      </td>

      {/* Trade count */}
      <td className="py-2 px-3 text-sm font-mono text-right tabular-nums">
        <span style={{ color: s.gates.trades_pass ? 'var(--color-green)' : undefined }}>
          {s.trade_count}
        </span>
      </td>

      {/* Paper Sharpe */}
      <td className="py-2 px-3 text-sm font-mono text-right tabular-nums">
        {fmtNum(s.sharpe, 3)}
      </td>

      {/* Δ vs Research */}
      <td className="py-2 px-3 text-sm font-mono text-right tabular-nums">
        <span
          style={{
            color:
              s.sharpe_delta == null
                ? 'var(--color-text-muted)'
                : s.gates.delta_pass
                ? 'var(--color-green)'
                : 'var(--color-red)',
          }}
        >
          {deltaStr}
        </span>
      </td>

      {/* Gates summary */}
      <td className="py-2 px-3">
        <div className="flex flex-wrap gap-1">
          <GateCell pass={s.gates.days_pass} label="30d" />
          <GateCell pass={s.gates.trades_pass} label="10tr" />
          <GateCell pass={s.gates.sharpe_pass} label="Sh≥0.3" />
          <GateCell pass={s.gates.delta_pass} label="|Δ|<0.5" />
        </div>
      </td>

      {/* Status */}
      <td className="py-2 px-3 text-sm whitespace-nowrap">
        <span style={{ color: statusColor }}>
          {statusEmoji} {statusLabel}
        </span>
      </td>
    </tr>
  )
}

export function PaperProgressPanel() {
  const { data, isLoading, isError } = usePaperProgress()

  if (isLoading) return <Skeleton className="h-32" />

  const strategies = data?.strategies ?? []
  const generatedAt = data?.generated_at

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl overflow-hidden dash-card">
      {/* Header */}
      <div className="px-4 py-3 border-b border-[var(--color-border)] flex items-center justify-between gap-2 flex-wrap">
        <div>
          <h3 className="text-sm font-semibold text-[var(--color-text)]">
            📊 Paper Strategy Progress
          </h3>
          <p className="text-xs text-[var(--color-text-muted)] mt-0.5">
            Promotion bar: ≥30d · ≥10 trades · Sharpe ≥0.3 · |Δ vs research| &lt; 0.5
          </p>
        </div>
        {generatedAt && (
          <span className="text-[10px] text-[var(--color-text-muted)] tabular-nums">
            {new Date(generatedAt).toLocaleTimeString()}
          </span>
        )}
      </div>

      {isError && (
        <div className="px-4 py-3 text-sm text-[var(--color-red)]">
          Failed to load paper progress data.
        </div>
      )}

      {!isError && strategies.length === 0 && (
        <div className="px-4 py-6 text-sm text-[var(--color-text-muted)] text-center">
          No strategies currently in PAPER state.
        </div>
      )}

      {strategies.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-[var(--color-border)] text-[10px] uppercase tracking-wider text-[var(--color-text-muted)]">
                <th className="py-2 px-3 font-medium">Strategy</th>
                <th className="py-2 px-3 font-medium text-right">Days</th>
                <th className="py-2 px-3 font-medium text-right">Trades</th>
                <th className="py-2 px-3 font-medium text-right">Sharpe</th>
                <th className="py-2 px-3 font-medium text-right">Δ Research</th>
                <th className="py-2 px-3 font-medium">Gates</th>
                <th className="py-2 px-3 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {strategies.map((s) => (
                <StrategyRow key={`${s.strategy}/${s.universe}`} s={s} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
