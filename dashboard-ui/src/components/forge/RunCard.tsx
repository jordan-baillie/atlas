/** A single run — collapsed row + click-to-expand full summary (hypothesis,
 *  data feasibility, and the full verdict metrics from the rails). */
import { useState } from 'react'
import type { ForgeCycle } from '../../api/forge-types'
import { fmtRelativeTime } from '../../lib/format'
import { C, Card, statusColor, statusLabel, fmtMetric } from './shared'

function Metric({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="px-2.5 py-1.5 rounded-lg bg-[var(--color-surface-alt)]">
      <div className="text-[9px] uppercase tracking-wide text-[var(--color-text-muted)]">{label}</div>
      <div className="text-sm font-bold tabular-nums" style={{ color: color || 'var(--color-text)' }}>{value}</div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  if (!children) return null
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[var(--color-text-muted)] mb-0.5">{label}</div>
      <div className="text-[12px] text-[var(--color-text)] leading-relaxed">{children}</div>
    </div>
  )
}

export function RunCard({ cycle }: { cycle: ForgeCycle }) {
  const [open, setOpen] = useState(false)
  const m = cycle.metrics
  const sc = statusColor(cycle.status)
  const icon = cycle.status === 'pass' ? '★' : cycle.status === 'error' ? '✕' : '·'
  const deg = m.degradation_pct

  return (
    <Card className="overflow-hidden">
      {/* collapsed row */}
      <button onClick={() => setOpen((v) => !v)} className="w-full text-left px-4 py-3 flex items-center gap-3 hover:bg-[var(--color-surface-alt)]/40 transition-colors">
        <span className="w-6 h-6 shrink-0 rounded-full flex items-center justify-center text-[11px] font-bold border"
          style={{ borderColor: sc, color: sc, background: cycle.status === 'pass' ? 'rgba(251,191,36,0.15)' : 'transparent' }}>{icon}</span>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-[var(--color-text)] truncate">{cycle.title}</div>
          <div className="text-[11px] text-[var(--color-text-muted)] flex items-center gap-2 flex-wrap">
            <span style={{ color: sc }} className="font-semibold">{statusLabel(cycle.status, cycle.tier)}</span>
            {m.search_sharpe != null && <span>search Sh {fmtMetric(m.search_sharpe)} → holdout {fmtMetric(m.holdout_sharpe)}</span>}
            {m.full_maxdd != null && <span>· maxDD {fmtMetric(m.full_maxdd, 'pct')}</span>}
            {m.n_trades != null && <span>· {m.n_trades} trades</span>}
            <span>· {fmtRelativeTime(cycle.ts)}</span>
          </div>
        </div>
        <span className="text-[var(--color-text-muted)] text-xs shrink-0 transition-transform" style={{ transform: open ? 'rotate(90deg)' : 'none' }}>▶</span>
      </button>

      {/* expanded summary */}
      {open && (
        <div className="px-4 pb-4 pt-1 border-t border-[var(--color-border)] space-y-4 forge-rise">
          {/* verdict metrics */}
          <div>
            <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)] mb-2 mt-3">Verdict — through the rails</div>
            <div className="grid grid-cols-3 sm:grid-cols-4 lg:grid-cols-6 gap-2">
              <Metric label="tier" value={cycle.tier || 'FAIL'} color={sc} />
              <Metric label="search Sharpe" value={fmtMetric(m.search_sharpe)} />
              <Metric label="holdout Sharpe" value={fmtMetric(m.holdout_sharpe)} color={m.holdout_pass ? C.green : C.iron} />
              <Metric label="degradation" value={deg != null ? `${deg}%` : '—'} color={deg != null && deg < -50 ? C.red : undefined} />
              <Metric label="full Sharpe" value={fmtMetric(m.full_sharpe)} />
              <Metric label="max DD" value={fmtMetric(m.full_maxdd, 'pct')} color={C.red} />
              <Metric label="trades" value={fmtMetric(m.n_trades, 'int')} />
              <Metric label="DSR" value={fmtMetric(m.dsr)} />
              <Metric label="CPCV" value={fmtMetric(m.median_cpcv)} />
              <Metric label="PBO" value={fmtMetric(m.pbo)} />
              <Metric label="deploy" value={m.deployment_passed == null ? '—' : m.deployment_passed ? 'pass' : 'fail'} color={m.deployment_passed ? C.green : C.red} />
              <Metric label="FDR bar" value={fmtMetric(m.promote_bar)} color={C.indigo} />
            </div>
            {m.holdout_reasons.length > 0 && (
              <div className="mt-2 text-[11px]" style={{ color: C.red }}>✕ {m.holdout_reasons.join(' · ')}</div>
            )}
          </div>

          {/* hypothesis */}
          <div className="grid md:grid-cols-2 gap-4">
            <div className="space-y-2.5">
              <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)]">Hypothesis</div>
              <Field label="premium">{cycle.premium}</Field>
              <Field label="market">{cycle.market}</Field>
              <Field label="signal approach">{cycle.hypothesis.signal_approach}</Field>
              <Field label="why not a duplicate">{cycle.hypothesis.why_not_duplicate}</Field>
              <Field label="pairs with">{cycle.hypothesis.pairs_with}</Field>
              {cycle.hypothesis.prior && (
                <div className="text-[11px] text-[var(--color-text-muted)]">prior conviction: <span className="text-[var(--color-text)] font-medium">{cycle.hypothesis.prior}</span></div>
              )}
            </div>
            <div className="space-y-2.5">
              <div className="text-[10px] uppercase tracking-widest text-[var(--color-text-muted)]">Data feasibility</div>
              <Field label="free / owned">{cycle.data.free_or_owned}</Field>
              <Field label="data source">{cycle.data.data_source}</Field>
              <Field label="gate-0 check">{cycle.data.gate0_data_check}</Field>
              {cycle.family && <div className="text-[11px] text-[var(--color-text-muted)]">FDR family: <code className="text-[var(--color-text)]">{cycle.family}</code></div>}
            </div>
          </div>
        </div>
      )}
    </Card>
  )
}
