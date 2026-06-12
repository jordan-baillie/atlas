import type { ReactNode } from 'react'
import { usePortfolioData, useSystemHealth, useLiveState } from '../../api/queries'
import type { LiveDeployed } from '../../api/queries'
import { Skeleton } from '../layout/Skeleton'
import { SectionBoundary } from '../layout/SectionBoundary'
import { SummaryStrip } from './SummaryStrip'
import { EquityChart } from './EquityChart'
import { PerformanceSection } from './PerformanceSection'
import { PositionsBook } from './PositionsBook'
import { OrdersTable } from './OrdersTable'
import { SystemHealth } from './SystemHealth'
import { SectionLabel } from '../ui/kit'
import { HudPanel, StreamDivider, Beacon } from '../ui/hud'
import { GlyphBook } from '../ui/glyphs'

function GroupDivider({ label }: { label: string }) {
  return (
    <div>
      <SectionLabel>{label}</SectionLabel>
      <StreamDivider className="mt-1" />
    </div>
  )
}

const STATE_COLOR: Record<string, string> = {
  shadow: 'var(--color-text-muted)', canary: 'var(--color-amber)', live: 'var(--color-green)',
}

// Deployed forge PASSes paper-trading into this book
function DeployedStrategies({ rows }: { rows: LiveDeployed[] }) {
  if (!rows.length) {
    return (
      <div className="rounded-lg border border-dashed border-[var(--color-border)] px-4 py-5 text-center text-sm text-[var(--color-text-muted)]">
        No strategies deployed yet. When a strategy clears every forge gate, it <b>auto-deploys here to paper-trade
        on live data</b> — accruing the forward-paper evidence required before any real capital (board 2026-06-09).
      </div>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs font-mono">
        <thead className="text-[var(--color-text-muted)] uppercase tracking-wider text-[10px]">
          <tr className="border-b border-[var(--color-border)]">
            <th className="text-left py-2 pr-4">Strategy</th><th className="text-left py-2 pr-4">Stage</th>
            <th className="text-right py-2 pr-4">Capital</th><th className="text-left py-2 pr-4">Approved</th>
            <th className="text-right py-2">Exp. Sharpe</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.name} className="border-b border-[var(--color-border)]/40">
              <td className="py-2 pr-4 text-[var(--color-text)]">{s.name}</td>
              <td className="py-2 pr-4">
                <span className="inline-flex items-center gap-1.5" style={{ color: STATE_COLOR[s.state] ?? 'var(--color-text)' }}>
                  <Beacon color={STATE_COLOR[s.state] ?? 'var(--color-text-muted)'} on={s.state !== 'shadow'} size={3.5} />
                  {s.state === 'shadow' ? 'paper' : s.state}
                </span>
              </td>
              <td className="py-2 pr-4 text-right tabular-nums">${s.capital.toLocaleString()}</td>
              <td className="py-2 pr-4">{s.approved ? '\u2705' : '\u2014'}</td>
              <td className="py-2 text-right tabular-nums">{s.expectation?.sharpe?.toFixed(2) ?? '\u2014'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CollapsibleGroup({ label, defaultOpen = false, children }: { label: string; defaultOpen?: boolean; children: ReactNode }) {
  return (
    <details open={defaultOpen} className="group">
      <summary className="flex items-center gap-2 py-2 cursor-pointer list-none select-none px-0.5">
        <span className="w-0.5 h-3.5 rounded-full bg-[var(--color-border)]" />
        <svg className="w-3.5 h-3.5 text-[var(--color-text-muted)] transition-transform duration-200 group-open:rotate-90" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
        </svg>
        <span className="text-[11px] uppercase tracking-widest text-[var(--color-text-muted)] font-semibold">{label}</span>
      </summary>
      <div className="space-y-4 md:space-y-6 mt-4">{children}</div>
    </details>
  )
}

export function PortfolioTab() {
  const portfolio = usePortfolioData()
  const health = useSystemHealth()
  const live = useLiveState()

  return (
    <div className="space-y-4 md:space-y-6 stagger-pop" data-section="paper">
      <div className="animate-in">
        <SectionBoundary title="Deployed strategies">
          <HudPanel
            title={<span className="flex items-center gap-1.5"><GlyphBook size={12} /> Forge Deployments</span>}
            brackets
          >
            {live.data ? <DeployedStrategies rows={live.data.deployed} /> : <Skeleton className="h-20" />}
          </HudPanel>
        </SectionBoundary>
      </div>

      <div className="animate-in">
        <SectionBoundary title="Summary">
          {portfolio.data?.account ? (
            <SummaryStrip
              account={portfolio.data.account}
              todayPnl={portfolio.data.summary?.today_pnl}
              positionsCount={portfolio.data.positions?.length ?? 0}
              maxPositions={portfolio.data.summary?.max_positions}
              asOf={portfolio.data.timestamp}
              marketOpen={portfolio.data.market_clock?.is_open === true}
            />
          ) : (
            <Skeleton className="h-28" />
          )}
        </SectionBoundary>
      </div>

      <div className="animate-in">
        <SectionBoundary title="Equity">
          {portfolio.data ? <EquityChart /> : <Skeleton className="h-96" />}
        </SectionBoundary>
      </div>

      <div className="animate-in">
        <GroupDivider label="Current State" />
      </div>

      <div className="animate-in">
        <SectionBoundary title="Positions">
          {portfolio.data?.positions ? (
            <PositionsBook positions={portfolio.data.positions} />
          ) : (
            <Skeleton className="h-48" />
          )}
        </SectionBoundary>
      </div>

      <div className="animate-in">
        <CollapsibleGroup label="Performance" defaultOpen={false}>
          <SectionBoundary title="Performance">
            {portfolio.data ? <PerformanceSection data={portfolio.data} /> : <Skeleton className="h-64" />}
          </SectionBoundary>
          <SectionBoundary title="Orders">
            {portfolio.data?.recent_orders ? <OrdersTable orders={portfolio.data.recent_orders} /> : <Skeleton className="h-32" />}
          </SectionBoundary>
        </CollapsibleGroup>
      </div>

      <div className="animate-in">
        <CollapsibleGroup label="System" defaultOpen={false}>
          <SectionBoundary title="Health">
            {health.data ? <SystemHealth data={health.data} /> : <Skeleton className="h-40" />}
          </SectionBoundary>
        </CollapsibleGroup>
      </div>
    </div>
  )
}
