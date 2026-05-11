import { useState } from 'react'
import { useAdminUniverses, useAdminStrategies } from '../../api/admin-queries'
import { useLifecycle } from '../../api/lifecycle'
import { SectionBoundary } from '../layout/SectionBoundary'
import { Skeleton } from '../layout/Skeleton'
import { UniverseRow } from './UniverseRow'
import { StrategyRow } from './StrategyRow'
import { RecentChangesPanel } from './RecentChangesPanel'
import type { StrategyAdminRow } from '../../api/admin-types'
import type { LifecycleRow } from '../../api/lifecycle'

// ── Section header utility ────────────────────────────────────────────────

function SectionHeader({ children }: { children: string }) {
  return (
    <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold mb-3">
      {children}
    </div>
  )
}

// ── Universes section ─────────────────────────────────────────────────────

function UniversesSection() {
  const { data, isLoading, error } = useAdminUniverses(true)

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <SectionHeader>Universes</SectionHeader>
      {isLoading && (
        <div className="space-y-2">
          <Skeleton className="h-12 rounded-lg" />
          <Skeleton className="h-12 rounded-lg" />
          <Skeleton className="h-12 rounded-lg" />
        </div>
      )}
      {error && (
        <div className="text-xs text-[var(--color-red)]">
          Failed: {(error as Error).message}
        </div>
      )}
      <div className="space-y-2">
        {data?.universes.map((u) => (
          <UniverseRow key={u.market_id} row={u} />
        ))}
      </div>
    </div>
  )
}

// ── Strategies section ────────────────────────────────────────────────────

function StrategiesSection() {
  const { data, isLoading, error } = useAdminStrategies(true)
  const { data: lcData } = useLifecycle(true)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  if (isLoading) {
    return (
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4">
        <Skeleton.Text lines={4} />
      </div>
    )
  }
  if (error) {
    return (
      <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 text-xs text-[var(--color-red)]">
        Failed: {(error as Error).message}
      </div>
    )
  }

  // Build lifecycle lookup map: "${strategy}.${universe}" → LifecycleRow
  const lcMap: Record<string, LifecycleRow> = {}
  for (const lr of lcData?.rows ?? []) {
    lcMap[`${lr.strategy}.${lr.universe}`] = lr
  }

  // Group by universe
  const byUniverse: Record<string, StrategyAdminRow[]> = {}
  for (const s of data?.strategies ?? []) {
    if (!byUniverse[s.market_id]) byUniverse[s.market_id] = []
    byUniverse[s.market_id].push(s)
  }
  const universeKeys = Object.keys(byUniverse).sort()

  const isExpanded = (k: string) => expanded[k] ?? k === universeKeys[0]

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card">
      <SectionHeader>Strategies (grouped by universe)</SectionHeader>
      <div className="space-y-3">
        {universeKeys.map((u) => {
          const rows = byUniverse[u]
          const open = isExpanded(u)
          return (
            <div key={u}>
              <button
                onClick={() => setExpanded({ ...expanded, [u]: !open })}
                className="w-full text-left text-xs font-mono font-semibold flex items-center gap-2 py-1 text-[var(--color-text)] hover:text-[var(--color-text-muted)] transition-colors"
              >
                <span>{open ? '▼' : '▶'}</span>
                <span>{u}</span>
                <span className="text-[var(--color-text-muted)] font-normal">
                  ({rows.length} strategies)
                </span>
              </button>
              {open && (
                <div className="space-y-1 mt-1 pl-4">
                  {rows.map((s) => (
                    <StrategyRow
                      key={`${s.market_id}.${s.strategy}`}
                      row={s}
                      lifecycleRow={lcMap[`${s.strategy}.${s.market_id}`]}
                    />
                  ))}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ── Main export ───────────────────────────────────────────────────────────

export function ControlsTab() {
  return (
    <div className="space-y-4 md:space-y-6">
      <SectionBoundary title="Universes">
        <UniversesSection />
      </SectionBoundary>
      <SectionBoundary title="Strategies">
        <StrategiesSection />
      </SectionBoundary>
      <SectionBoundary title="Recent Changes">
        <RecentChangesPanel />
      </SectionBoundary>
    </div>
  )
}
