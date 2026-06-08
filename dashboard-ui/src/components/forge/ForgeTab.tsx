/**
 * ForgeTab — live "mission control" for the Hephaestus autonomous research loop.
 *
 * Ships FOUR selectable design variants (A/B/C/D) behind a segmented switcher so
 * the operator can compare them live and pick one. After selection, drop the
 * three unused variant files + the switcher and render the winner directly.
 */
import { useState } from 'react'
import { useForgeState } from '../../api/forge-queries'
import { useUrlState } from '../../hooks/useUrlState'
import { Skeleton } from '../layout/Skeleton'
import { VariantForge } from './VariantForge'
import { VariantMissionControl } from './VariantMissionControl'
import { VariantGauntlet } from './VariantGauntlet'
import { VariantNotebook } from './VariantNotebook'

type Variant = 'A' | 'B' | 'C' | 'D'

const VARIANTS: Array<{ id: Variant; label: string; emoji: string; desc: string }> = [
  { id: 'A', label: 'The Forge', emoji: '⚒️', desc: 'Live pipeline in motion' },
  { id: 'B', label: 'Mission Control', emoji: '🛰️', desc: 'Telemetry gauges + countdown' },
  { id: 'C', label: 'The Gauntlet', emoji: '🪜', desc: 'Hypotheses falling through the gates' },
  { id: 'D', label: 'Lab Notebook', emoji: '📓', desc: 'The knowledge story' },
]

export function ForgeTab() {
  const [urlVariant, setUrlVariant] = useUrlState<string | null>('forge', 'A')
  const initial = (['A', 'B', 'C', 'D'].includes(urlVariant || '') ? urlVariant : 'A') as Variant
  const [variant, setVariant] = useState<Variant>(initial)
  const q = useForgeState()

  const select = (v: Variant) => { setVariant(v); setUrlVariant(v) }

  return (
    <div className="space-y-4">
      {/* Variant switcher — temporary, for picking */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-2 justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-[var(--color-text)]">🔥 Forge Monitor</span>
          <span className="text-[11px] text-[var(--color-text-muted)] hidden md:inline">— pick a design below</span>
        </div>
        <div className="flex flex-wrap gap-1 p-1 bg-[var(--color-surface)] border border-[var(--color-border)] rounded-lg">
          {VARIANTS.map((v) => {
            const active = variant === v.id
            return (
              <button
                key={v.id}
                onClick={() => select(v.id)}
                title={v.desc}
                className={[
                  'px-2.5 py-1.5 rounded-md text-xs transition-all inline-flex items-center gap-1.5',
                  active
                    ? 'bg-[var(--color-surface-alt)] text-[var(--color-text)] font-semibold ring-1 ring-[var(--color-amber)]/40'
                    : 'text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
                ].join(' ')}
              >
                <span>{v.emoji}</span>
                <span className="hidden sm:inline">{v.id} · {v.label}</span>
                <span className="sm:hidden">{v.id}</span>
              </button>
            )
          })}
        </div>
      </div>

      {q.isLoading && !q.data ? (
        <div className="space-y-4">
          <Skeleton className="h-32" />
          <Skeleton className="h-80" />
        </div>
      ) : q.isError ? (
        <div className="p-6 text-center text-sm text-[var(--color-negative)] bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl">
          Couldn’t load forge state. Is the loop wired? <code className="text-xs">/api/forge/state</code>
        </div>
      ) : q.data ? (
        <div key={variant} className="forge-rise">
          {variant === 'A' ? <VariantForge state={q.data} />
            : variant === 'B' ? <VariantMissionControl state={q.data} />
              : variant === 'C' ? <VariantGauntlet state={q.data} />
                : <VariantNotebook state={q.data} />}
        </div>
      ) : null}
    </div>
  )
}
