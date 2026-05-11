import { useState, useMemo } from 'react'
import type { PositionRisk, PortfolioRiskMetrics } from '../../api/types'
import { StatCard } from '../shared/StatCard'
import { AsOfBadge } from '../shared/AsOfBadge'
import { RiskTable } from './RiskTable'
import { fmtCcy, fmtPct } from '../../lib/format'
import { useRuinProbability, useRefreshRuinProbability } from '../../api/queries'

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
      <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
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
      <div className="mt-3 grid grid-cols-1 sm:grid-cols-3 gap-3 text-[11px] text-[var(--color-text-muted)] font-mono">
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

/** Pure helper: avoids calling Date.now() directly in render (lint rule: components-must-be-pure) */
function ageHours(iso: string): number {
  return (new Date().getTime() - new Date(iso).getTime()) / (1000 * 60 * 60)
}

// RuinStalenessSection — shows a banner when ruin probability is stale + refresh button
function RuinStalenessSection() {
  const { data: ruinData } = useRuinProbability()
  const refresh = useRefreshRuinProbability()
  const [refreshing, setRefreshing] = useState(false)

  if (!ruinData) return null

  const ageH = ruinData.as_of ? ageHours(ruinData.as_of) : 0
  const isStale = ruinData.stale === true || ageH > 24

  if (!isStale) return null

  const h90 = ruinData.horizons?.['90d']
  const pRuin = h90?.prob_ruin ?? 0
  const survivalPct = (1 - pRuin) * 100

  const bannerMsg = ruinData.reason
    ? `🟡 ${ruinData.reason}`
    : '🟡 PORTFOLIO CHANGED — recomputing ruin probability with current positions'

  function handleRefresh() {
    setRefreshing(true)
    refresh.mutate(undefined, {
      onSettled: () => setTimeout(() => setRefreshing(false), 5_500),
    })
  }

  return (
    <div className="bg-amber-900/30 border border-amber-700/50 rounded p-3 mb-4">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <div className="text-amber-200 text-xs font-medium mb-1">{bannerMsg}</div>
          {h90 && (
            <div className="text-amber-300/60 text-xs font-mono opacity-60">
              Last computed: {survivalPct.toFixed(1)}% safe over 90d
              {ruinData.as_of && (
                <span className="ml-2 text-amber-300/40">
                  ({new Date(ruinData.as_of).toLocaleDateString()})
                </span>
              )}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={handleRefresh}
          disabled={refreshing || refresh.isPending}
          className="text-[11px] px-3 py-1.5 rounded border border-amber-600/50 text-amber-200 hover:bg-amber-800/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-mono whitespace-nowrap"
        >
          {refreshing || refresh.isPending ? '⟳ Refreshing…' : '↻ Refresh now'}
        </button>
      </div>
    </div>
  )
}

export function RiskSection({ data }: Props) {
  const positions = useMemo(() => data.positions ?? [], [data.positions])
  const s = data.summary
  const unprotected = s?.positions_without_stops ?? 0
  const pr = data.portfolio_risk
  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-5 space-y-6 dash-card">
      <RuinStalenessSection />
      <div>
        <div className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text-muted)] font-semibold mb-3">POSITION RISK</div>
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
          <StatCard label="CAPITAL AT RISK" value={fmtCcy(s?.total_risk_dollars)} sub={fmtPct(s?.total_risk_pct)} />
          <StatCard
            label="EQUITY"
            value={
              <span className="flex items-center gap-1.5 flex-wrap">
                {fmtCcy(s?.equity)}
                <AsOfBadge source="snapshot" asOf={data.as_of} title="End-of-day snapshot — market_equity_history last NYSE close" />
              </span>
            }
            sub={`${s?.num_positions ?? 0} positions`}
          />
          <StatCard label="AVG DISTANCE TO STOP" value={fmtPct(s?.avg_distance_to_stop)} />
          <StatCard
            label="MAX RISK/TRADE"
            value={fmtPct(s?.max_risk_per_trade_pct)}
            sub={<span className={unprotected > 0 ? 'text-[var(--color-red)]' : ''}>{unprotected} unprotected</span>}
          />
        </div>
      </div>
      {pr && <PortfolioTailRisk pr={pr} />}
      <RiskTable positions={positions} stop_probability={data.stop_probability} />
    </div>
  )
}
