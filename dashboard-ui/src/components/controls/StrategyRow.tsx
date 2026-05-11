import { useState } from 'react'
import { useChangeStrategyState } from '../../api/admin-queries'
import { useInvalidateLifecycle } from '../../api/lifecycle'
import { ChangeStateModal } from './ChangeStateModal'
import { LifecycleTransitionModal } from './ChangeStateModal'
import { LifecycleActions } from './LifecycleActions'
import { LifecycleHistoryModal } from './LifecycleHistoryModal'
import { RevertButton } from './RevertButton'
import { Badge } from '../shared/Badge'
import { fmtNum, fmtSignedCcy } from '../../lib/format'
import type { StrategyAdminRow } from '../../api/admin-types'
import type { LifecycleRow, LifecycleActionType } from '../../api/lifecycle'
import type { BadgeVariant } from '../shared/Badge'

// ── Lifecycle badge helpers ───────────────────────────────────────────────

function lcVariant(state: string): BadgeVariant {
  if (state === 'RESEARCH') return 'info'
  if (state === 'PAPER') return 'warning'
  if (state === 'LIVE') return 'success'
  return 'neutral'  // RETIRED + unknown
}

// ── Gap color helper ──────────────────────────────────────────────────────

function gapVariant(gap: number | null | undefined): BadgeVariant {
  if (gap == null) return 'neutral'
  if (gap > 0.5) return 'danger'
  if (gap > 0.3) return 'warning'
  return 'success'
}

function m(v: number | null | undefined, digits = 2): string {
  return v != null ? fmtNum(v, digits) : '—'
}

// ── Per-state inline metrics ──────────────────────────────────────────────

function LifecycleMetrics({ lr }: { lr: LifecycleRow }) {
  switch (lr.state) {
    case 'RESEARCH':
      return lr.research_sharpe != null ? (
        <span className="text-[10px] text-[var(--color-text-muted)] font-mono tabular-nums">
          Research σ {m(lr.research_sharpe)}
        </span>
      ) : null

    case 'PAPER': {
      return (
        <span className="text-[10px] font-mono tabular-nums flex items-center gap-1.5">
          <span className="text-[var(--color-text-muted)]">Paper σ </span>
          <span className="text-[var(--color-text)]">{m(lr.paper_sharpe)}</span>
          {lr.gap != null && (
            <Badge variant={gapVariant(lr.gap)} size="xs">gap {m(lr.gap)}</Badge>
          )}
        </span>
      )
    }

    case 'LIVE':
      return lr.live_sharpe != null ? (
        <span className="text-[10px] text-[var(--color-text-muted)] font-mono tabular-nums">
          Live σ <span className="text-[var(--color-text)]">{m(lr.live_sharpe)}</span>
          {lr.live_trades_count != null && (
            <> · {m(lr.live_trades_count, 0)} trades</>
          )}
        </span>
      ) : null

    default:
      return null
  }
}

// ── Component ─────────────────────────────────────────────────────────────

interface Props {
  row: StrategyAdminRow
  lifecycleRow?: LifecycleRow
}

export function StrategyRow({ row, lifecycleRow }: Props) {
  const [overrideOpen, setOverrideOpen] = useState(false)
  const mutation = useChangeStrategyState()

  const overrideExpiringSoon = row.override?.expires_at
    ? (new Date(row.override.expires_at).getTime() - Date.now()) < 7 * 24 * 3600 * 1000
    : false
  const currentState = row.effective_enabled ? 'enabled' : 'disabled'

  async function handleSubmit(req: {
    state: string
    reason: string
    expires_at: string | null | undefined
    confirm_token?: string
    i_understand: boolean
  }) {
    await mutation.mutateAsync({
      market_id: row.market_id,
      strategy: row.strategy,
      body: {
        state: req.state as 'enabled' | 'disabled',
        reason: req.reason,
        expires_at: req.expires_at,
        i_understand: req.i_understand,
      },
    })
  }

  const [historyOpen, setHistoryOpen] = useState(false)
  const [activeAction, setActiveAction] = useState<LifecycleActionType | null>(null)
  const invalidateLifecycle = useInvalidateLifecycle()

  function handleLifecycleAction(action: LifecycleActionType) {
    setActiveAction(action)
  }

  function closeLifecycleModal() {
    setActiveAction(null)
  }

  return (
    <div className="border border-[var(--color-border)]/50 rounded-md px-3 py-2 flex items-start justify-between gap-3 text-sm flex-wrap hover:bg-[var(--color-surface-alt)]/20 transition-colors">

      {/* Left: identity + enabled/disabled */}
      <div className="flex items-center gap-3 flex-wrap">
        <span className="font-mono min-w-[200px]">{row.strategy}</span>

        {/* Enabled / disabled badge */}
        <Badge variant={row.effective_enabled ? 'success' : 'neutral'} size="xs" dot>
          {row.effective_enabled ? 'ENABLED' : 'DISABLED'}
        </Badge>

        <span className="text-xs text-[var(--color-text-muted)] font-mono tabular-nums">
          w={row.weight.toFixed(2)}
        </span>

        {row.override && (
          <Badge
            variant={overrideExpiringSoon ? 'warning' : 'neutral'}
            size="xs"
            title={`Reason: ${row.override.reason ?? '—'}\nBy: ${row.override.created_by}\nAt: ${row.override.created_at}\nExpires: ${row.override.expires_at ?? 'never'}`}
          >
            override
          </Badge>
        )}
      </div>

      {/* Right: metrics + badges + actions */}
      <div className="flex items-center gap-3 text-xs text-[var(--color-text-muted)] flex-wrap">
        {/* 30d stats */}
        <span className="font-mono tabular-nums">
          {row.trades_30d} trades · {fmtSignedCcy(row.pnl_30d)} 30d
        </span>

        {/* Legacy lifecycle field */}
        <Badge variant="neutral" size="xs">{row.lifecycle}</Badge>

        {/* New lifecycle state badge — clickable → history modal */}
        {lifecycleRow && (
          <>
            <button
              onClick={() => setHistoryOpen(true)}
              title="Click to see lifecycle history"
              className="focus:outline-none focus:ring-2 focus:ring-[var(--color-border)] rounded-full"
              data-testid="lifecycle-state-badge"
            >
              <Badge variant={lcVariant(lifecycleRow.state)} size="xs" dot>
                {lifecycleRow.state}
              </Badge>
            </button>
            <LifecycleMetrics lr={lifecycleRow} />
          </>
        )}

        {/* Action buttons */}
        {row.override && <RevertButton overrideId={row.override.id} />}

        <button
          onClick={() => setOverrideOpen(true)}
          className="h-8 px-2.5 rounded-md bg-[var(--color-surface-alt)] hover:bg-[var(--color-border)]
                     border border-[var(--color-border)] text-xs text-[var(--color-text-muted)]
                     hover:text-[var(--color-text)] transition-colors"
        >
          Toggle
        </button>

        {lifecycleRow && (
          <LifecycleActions
            row={lifecycleRow}
            onAction={handleLifecycleAction}
            disabled={false}
          />
        )}
      </div>

      {/* Config override modal */}
      <ChangeStateModal
        open={overrideOpen}
        onClose={() => setOverrideOpen(false)}
        scope="strategy"
        marketId={row.market_id}
        strategyName={row.strategy}
        currentState={currentState}
        currentSource={row.override ? 'override' : 'config'}
        isProduction={false}
        openPositions={row.open_positions}
        trades30d={row.trades_30d}
        pnl30d={row.pnl_30d}
        lifecycle={row.lifecycle}
        onSubmit={handleSubmit}
      />

      {/* Lifecycle history modal */}
      {lifecycleRow && (
        <LifecycleHistoryModal
          strategy={lifecycleRow.strategy}
          universe={lifecycleRow.universe}
          open={historyOpen}
          onClose={() => setHistoryOpen(false)}
        />
      )}

      {/* Lifecycle transition modal */}
      {lifecycleRow && activeAction && (
        <LifecycleTransitionModal
          open={activeAction !== null}
          onClose={closeLifecycleModal}
          action={activeAction}
          row={lifecycleRow}
          onSuccess={invalidateLifecycle}
        />
      )}
    </div>
  )
}
