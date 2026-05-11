import { useState } from 'react'
import { useOverrideAudit } from '../../api/admin-queries'
import { useRecentLifecycleHistory } from '../../api/lifecycle'
import { RevertButton } from './RevertButton'
import { Badge } from '../shared/Badge'
import { fmtRelativeTime } from '../../lib/format'
import type { AuditEntry } from '../../api/admin-types'
import type { RecentHistoryEntry } from '../../api/lifecycle'
import type { BadgeVariant } from '../shared/Badge'

// ── Config override audit entries ─────────────────────────────────────────

function actionVariant(action: string): BadgeVariant {
  if (action === 'create') return 'info'
  if (action === 'supersede') return 'warning'
  return 'neutral'
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  const [expanded, setExpanded] = useState(false)
  const reason = entry.reason ?? ''
  const truncated = reason.length > 80 ? reason.slice(0, 80) + '…' : reason
  const actor = entry.actor.startsWith('human:') ? entry.actor.slice(6) : entry.actor

  return (
    <div className="border-b border-[var(--color-border)]/40 py-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <span title={entry.ts} className="text-[var(--color-text-muted)] font-mono tabular-nums min-w-[80px]">
          {fmtRelativeTime(entry.ts)}
        </span>
        <Badge variant={actionVariant(entry.action)} size="xs">{entry.action}</Badge>
        <span className="text-[var(--color-text-muted)]">{actor}</span>
        <span className="font-mono">
          {entry.scope} {entry.key}
        </span>
        <span className="text-[var(--color-text-muted)]">
          {entry.from_state ?? '—'} → {entry.to_state ?? '—'}
        </span>
        {entry.action === 'create' && entry.override_id != null && (
          <RevertButton overrideId={entry.override_id} label="Revert" />
        )}
      </div>
      {reason && (
        <div
          className="mt-1 text-[var(--color-text-muted)] cursor-pointer pl-2"
          onClick={() => setExpanded(!expanded)}
          title="Click to expand"
        >
          Reason: {expanded ? reason : truncated}
        </div>
      )}
    </div>
  )
}

// ── Lifecycle change entries ──────────────────────────────────────────────

function lcStateVariant(state: string): BadgeVariant {
  if (state === 'RESEARCH') return 'info'
  if (state === 'PAPER') return 'warning'
  if (state === 'LIVE') return 'success'
  return 'neutral'  // RETIRED
}

function LifecycleRow({ entry }: { entry: RecentHistoryEntry }) {
  const [expanded, setExpanded] = useState(false)
  const reason = entry.reason ?? ''
  const truncated = reason.length > 80 ? reason.slice(0, 80) + '…' : reason

  return (
    <div className="border-b border-[var(--color-border)]/40 py-2 text-xs">
      <div className="flex items-center gap-2 flex-wrap">
        <span
          title={entry.transitioned_at}
          className="text-[var(--color-text-muted)] font-mono tabular-nums min-w-[80px]"
        >
          {fmtRelativeTime(entry.transitioned_at)}
        </span>

        {/* Source pill */}
        <Badge variant="accent" size="xs">lifecycle</Badge>

        {entry.operator && (
          <span className="text-[var(--color-text-muted)]">{entry.operator}</span>
        )}
        <span className="font-mono">
          {entry.strategy} · {entry.universe}
        </span>

        {/* State transition badges */}
        <span className="flex items-center gap-1">
          {entry.from_state && (
            <>
              <Badge variant={lcStateVariant(entry.from_state)} size="xs">{entry.from_state}</Badge>
              <span className="text-[var(--color-text-muted)]">→</span>
            </>
          )}
          <Badge variant={lcStateVariant(entry.to_state)} size="xs">{entry.to_state}</Badge>
        </span>

        {entry.auto_promotion_id != null && (
          <span className="text-[var(--color-text-muted)] text-[10px]">
            auto #{entry.auto_promotion_id}
          </span>
        )}
      </div>
      {reason && (
        <div
          className="mt-1 text-[var(--color-text-muted)] cursor-pointer pl-2"
          onClick={() => setExpanded(!expanded)}
          title="Click to expand"
        >
          Reason: {expanded ? reason : truncated}
        </div>
      )}
    </div>
  )
}

// ── Panel ─────────────────────────────────────────────────────────────────

export function RecentChangesPanel() {
  const { data, isLoading, error } = useOverrideAudit({ limit: 50 })
  const { data: lcData, isLoading: lcLoading, error: lcError } = useRecentLifecycleHistory(true)

  return (
    <div className="bg-[var(--color-surface)] border border-[var(--color-border)] rounded-xl p-4 dash-card space-y-4">

      {/* Config override audit */}
      <section>
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold mb-3">
          Config override changes (last 50)
        </div>
        {isLoading && (
          <div className="text-xs text-[var(--color-text-muted)]">Loading audit log…</div>
        )}
        {error && (
          <div className="text-xs text-[var(--color-red)]">
            Failed to load: {(error as Error).message}
          </div>
        )}
        {data?.audit.length === 0 && (
          <div className="text-xs text-[var(--color-text-muted)]">No override changes yet.</div>
        )}
        {data?.audit.map((entry) => (
          <AuditRow key={entry.id} entry={entry} />
        ))}
      </section>

      {/* Lifecycle transitions */}
      <section>
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-text-muted)] font-semibold mb-3">
          Lifecycle changes (last 20)
        </div>
        {lcLoading && (
          <div className="text-xs text-[var(--color-text-muted)]">Loading lifecycle history…</div>
        )}
        {lcError && (
          <div className="text-xs text-[var(--color-red)]">
            Failed to load: {(lcError as Error).message}
          </div>
        )}
        {lcData === null && !lcLoading && !lcError && (
          <div className="text-xs text-[var(--color-text-muted)]">
            Lifecycle history endpoint not available yet.
          </div>
        )}
        {lcData?.history.length === 0 && (
          <div className="text-xs text-[var(--color-text-muted)]">No lifecycle transitions yet.</div>
        )}
        {lcData?.history.map((entry, idx) => (
          <LifecycleRow key={`${entry.strategy}.${entry.universe}.${idx}`} entry={entry} />
        ))}
      </section>
    </div>
  )
}
