import type { PositionRisk, PortfolioRiskMetrics } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { RiskTable } from './RiskTable'
import { fmtCcy, fmtPct } from '../../lib/format'

interface Props { data: PositionRisk }

const REGIME_LABELS: Record<string, string> = {
  bull_risk_on: 'Bull Risk-On',
  bull_risk_off: 'Bull Risk-Off',
  transition_uncertain: 'Transition',
  bear_risk_off: 'Bear Risk-Off',
  bear_capitulation: 'Bear Capitulation',
  recovery_early: 'Recovery',
}

function fmtRegime(state?: string): string {
  if (!state) return '\u2014'
  return REGIME_LABELS[state] ?? state
}

function PortfolioTailRisk({ pr }: { pr: PortfolioRiskMetrics }) {
  const h1 = pr.horizons?.['1d']
  const h5 = pr.horizons?.['5d']
  const var95Pct = h1?.var_95_pct != null ? `${(h1.var_95_pct * 100).toFixed(2)}%` : null

  return (
    <div>
      <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">
        PORTFOLIO TAIL RISK · {pr.method ?? 'unknown'} · {fmtRegime(pr.current_regime)}
      </div>
      <div className="grid grid-cols-4 md:grid-cols-2 sm:grid-cols-1 gap-4">
        <StatCard
          label="1-DAY VAR 95%"
          value={fmtCcy(h1?.var_95)}
          sub={var95Pct ?? undefined}
        />
        <StatCard
          label="1-DAY CVAR 95%"
          value={fmtCcy(h1?.cvar_95)}
          sub="expected shortfall"
        />
        <StatCard
          label="5-DAY VAR 95%"
          value={fmtCcy(h5?.var_95)}
          sub={h5?.var_95_pct != null ? `${(h5.var_95_pct * 100).toFixed(2)}%` : undefined}
        />
        <StatCard
          label="5-DAY CVAR 95%"
          value={fmtCcy(h5?.cvar_95)}
          sub="expected shortfall"
        />
      </div>
      <div className="mt-3 grid grid-cols-3 sm:grid-cols-1 gap-3 text-[11px] text-[var(--color-text-muted)] font-mono">
        <div>
          <span className="uppercase tracking-wide">EFFECTIVE BETS</span>
          <div className="text-[var(--color-text)] text-base">{pr.effective_bets?.toFixed(2) ?? '\u2014'}</div>
        </div>
        <div>
          <span className="uppercase tracking-wide">CORR (AVG / MAX)</span>
          <div className="text-[var(--color-text)] text-base">
            {pr.correlation_avg?.toFixed(2) ?? '\u2014'} / {pr.correlation_max?.toFixed(2) ?? '\u2014'}
          </div>
        </div>
        <div>
          <span className="uppercase tracking-wide">SIM PATHS</span>
          <div className="text-[var(--color-text)] text-base">{pr.n_paths?.toLocaleString() ?? '\u2014'}</div>
        </div>
      </div>
    </div>
  )
}

export function RiskSection({ data }: Props) {
  const s = data.summary
  const unprotected = s?.positions_without_stops ?? 0
  const pr = data.portfolio_risk
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 space-y-6 dash-card">
      <div>
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">POSITION RISK</div>
        <div className="grid grid-cols-4 md:grid-cols-2 sm:grid-cols-1 gap-4">
          <StatCard label="CAPITAL AT RISK" value={fmtCcy(s?.total_risk_dollars)} sub={fmtPct(s?.total_risk_pct)} />
          <StatCard label="EQUITY" value={fmtCcy(s?.equity)} sub={`${s?.num_positions ?? 0} positions`} />
          <StatCard label="AVG DISTANCE TO STOP" value={fmtPct(s?.avg_distance_to_stop)} />
          <StatCard
            label="MAX RISK/TRADE"
            value={fmtPct(s?.max_risk_per_trade_pct)}
            sub={<span className={unprotected > 0 ? 'text-[var(--color-red)]' : ''}>{unprotected} unprotected</span>}
          />
        </div>
      </div>
      {pr && <PortfolioTailRisk pr={pr} />}
      <RiskTable positions={data.positions ?? []} stop_probability={data.stop_probability} />
    </div>
  )
}
